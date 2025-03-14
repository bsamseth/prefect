"""
Module containing the base workflow class and decorator - for most use cases, using the [`@flow` decorator][prefect.flows.flow] is preferred.
"""
# This file requires type-checking with pyright because mypy does not yet support PEP612
# See https://github.com/python/mypy/issues/8645

import inspect
import os
import warnings
from functools import partial, update_wrapper
from tempfile import NamedTemporaryFile
from typing import (
    Any,
    AnyStr,
    Awaitable,
    Callable,
    Coroutine,
    Dict,
    Generic,
    Iterable,
    List,
    NoReturn,
    Optional,
    Type,
    TypeVar,
    Union,
    cast,
    overload,
)

import pydantic
from fastapi.encoders import jsonable_encoder
from pydantic.decorator import ValidatedFunction
from typing_extensions import Literal, ParamSpec

from prefect.context import PrefectObjectRegistry, registry_from_script
from prefect.exceptions import (
    MissingFlowError,
    ParameterTypeError,
    UnspecifiedFlowError,
)
from prefect.futures import PrefectFuture
from prefect.logging import get_logger
from prefect.results import ResultSerializer, ResultStorage
import prefect.server.schemas as schemas
from prefect.client.schemas import FlowRun
from prefect.settings import (
    PREFECT_FLOW_DEFAULT_RETRIES,
    PREFECT_FLOW_DEFAULT_RETRY_DELAY_SECONDS,
)
from prefect.states import State
from prefect.task_runners import BaseTaskRunner, ConcurrentTaskRunner
from prefect.utilities.annotations import NotSet
from prefect.utilities.asyncutils import is_async_fn
from prefect.utilities.callables import (
    get_call_parameters,
    parameter_schema,
    parameters_to_args_kwargs,
    raise_for_reserved_arguments,
)
from prefect.utilities.collections import listrepr
from prefect.utilities.hashing import file_hash
from prefect.utilities.importtools import import_object

T = TypeVar("T")  # Generic type var for capturing the inner return type of async funcs
R = TypeVar("R")  # The return type of the user's function
P = ParamSpec("P")  # The parameters of the flow

logger = get_logger("flows")


@PrefectObjectRegistry.register_instances
class Flow(Generic[P, R]):
    """
    A Prefect workflow definition.

    !!! note
        We recommend using the [`@flow` decorator][prefect.flows.flow] for most use-cases.

    Wraps a function with an entrypoint to the Prefect engine. To preserve the input
    and output types, we use the generic type variables `P` and `R` for "Parameters" and
    "Returns" respectively.

    Args:
        fn: The function defining the workflow.
        name: An optional name for the flow; if not provided, the name will be inferred
            from the given function.
        version: An optional version string for the flow; if not provided, we will
            attempt to create a version string as a hash of the file containing the
            wrapped function; if the file cannot be located, the version will be null.
        flow_run_name: An optional name to distinguish runs of this flow; this name can
            be provided as a string template with the flow's parameters as variables,
            or a function that returns a string.
        task_runner: An optional task runner to use for task execution within the flow;
            if not provided, a `ConcurrentTaskRunner` will be used.
        description: An optional string description for the flow; if not provided, the
            description will be pulled from the docstring for the decorated function.
        timeout_seconds: An optional number of seconds indicating a maximum runtime for
            the flow. If the flow exceeds this runtime, it will be marked as failed.
            Flow execution may continue until the next task is called.
        validate_parameters: By default, parameters passed to flows are validated by
            Pydantic. This will check that input values conform to the annotated types
            on the function. Where possible, values will be coerced into the correct
            type; for example, if a parameter is defined as `x: int` and "5" is passed,
            it will be resolved to `5`. If set to `False`, no validation will be
            performed on flow parameters.
        retries: An optional number of times to retry on flow run failure.
        retry_delay_seconds: An optional number of seconds to wait before retrying the
            flow after failure. This is only applicable if `retries` is nonzero.
        persist_result: An optional toggle indicating whether the result of this flow
            should be persisted to result storage. Defaults to `None`, which indicates
            that Prefect should choose whether the result should be persisted depending on
            the features being used.
        result_storage: An optional block to use to perist the result of this flow.
            This value will be used as the default for any tasks in this flow.
            If not provided, the local file system will be used unless called as
            a subflow, at which point the default will be loaded from the parent flow.
        result_serializer: An optional serializer to use to serialize the result of this
            flow for persistence. This value will be used as the default for any tasks
            in this flow. If not provided, the value of `PREFECT_RESULTS_DEFAULT_SERIALIZER`
            will be used unless called as a subflow, at which point the default will be
            loaded from the parent flow.
        on_failure: An optional list of callables to run when the flow enters a failed state.
        on_completion: An optional list of callables to run when the flow enters a completed state.
        on_cancellation: An optional list of callables to run when the flow enters a cancelling state.
        on_crashed: An optional list of callables to run when the flow enters a crashed state.
    """

    # NOTE: These parameters (types, defaults, and docstrings) should be duplicated
    #       exactly in the @flow decorator
    def __init__(
        self,
        fn: Callable[P, R],
        name: Optional[str] = None,
        version: Optional[str] = None,
        flow_run_name: Optional[Union[Callable[[], str], str]] = None,
        retries: Optional[int] = None,
        retry_delay_seconds: Optional[Union[int, float]] = None,
        task_runner: Union[Type[BaseTaskRunner], BaseTaskRunner] = ConcurrentTaskRunner,
        description: str = None,
        timeout_seconds: Union[int, float] = None,
        validate_parameters: bool = True,
        persist_result: Optional[bool] = None,
        result_storage: Optional[ResultStorage] = None,
        result_serializer: Optional[ResultSerializer] = None,
        cache_result_in_memory: bool = True,
        log_prints: Optional[bool] = None,
        on_completion: Optional[
            List[Callable[[schemas.core.Flow, FlowRun, State], None]]
        ] = None,
        on_failure: Optional[
            List[Callable[[schemas.core.Flow, FlowRun, State], None]]
        ] = None,
        on_cancellation: Optional[
            List[Callable[[schemas.core.Flow, FlowRun, State], None]]
        ] = None,
        on_crashed: Optional[
            List[Callable[[schemas.core.Flow, FlowRun, State], None]]
        ] = None,
    ):
        if not callable(fn):
            raise TypeError("'fn' must be callable")

        # Validate name if given
        if name:
            schemas.core.raise_on_invalid_name(name)

        self.name = name or fn.__name__.replace("_", "-")

        if flow_run_name is not None:
            if not isinstance(flow_run_name, str) and not callable(flow_run_name):
                raise TypeError(
                    "Expected string or callable for 'flow_run_name'; got"
                    f" {type(flow_run_name).__name__} instead."
                )
        self.flow_run_name = flow_run_name

        task_runner = task_runner or ConcurrentTaskRunner()
        self.task_runner = (
            task_runner() if isinstance(task_runner, type) else task_runner
        )

        self.log_prints = log_prints

        self.description = description or inspect.getdoc(fn)
        update_wrapper(self, fn)
        self.fn = fn
        self.isasync = is_async_fn(self.fn)

        raise_for_reserved_arguments(self.fn, ["return_state", "wait_for"])

        # Version defaults to a hash of the function's file
        flow_file = inspect.getsourcefile(self.fn)
        if not version:
            try:
                version = file_hash(flow_file)
            except (FileNotFoundError, TypeError, OSError):
                pass  # `getsourcefile` can return null values and "<stdin>" for objects in repls
        self.version = version

        self.timeout_seconds = float(timeout_seconds) if timeout_seconds else None

        # FlowRunPolicy settings
        # TODO: We can instantiate a `FlowRunPolicy` and add Pydantic bound checks to
        #       validate that the user passes positive numbers here
        self.retries = (
            retries if retries is not None else PREFECT_FLOW_DEFAULT_RETRIES.value()
        )

        self.retry_delay_seconds = (
            retry_delay_seconds
            if retry_delay_seconds is not None
            else PREFECT_FLOW_DEFAULT_RETRY_DELAY_SECONDS.value()
        )

        self.parameters = parameter_schema(self.fn)
        self.should_validate_parameters = validate_parameters

        if self.should_validate_parameters:
            # Try to create the validated function now so that incompatibility can be
            # raised at declaration time rather than at runtime
            # We cannot, however, store the validated function on the flow because it
            # is not picklable in some environments
            try:
                ValidatedFunction(self.fn, config=None)
            except pydantic.ConfigError as exc:
                raise ValueError(
                    "Flow function is not compatible with `validate_parameters`. "
                    "Disable validation or change the argument names."
                ) from exc

        self.persist_result = persist_result
        self.result_storage = result_storage
        self.result_serializer = result_serializer
        self.cache_result_in_memory = cache_result_in_memory

        # Check for collision in the registry
        registry = PrefectObjectRegistry.get()

        if registry and any(
            other
            for other in registry.get_instances(Flow)
            if other.name == self.name and id(other.fn) != id(self.fn)
        ):
            file = inspect.getsourcefile(self.fn)
            line_number = inspect.getsourcelines(self.fn)[1]
            warnings.warn(
                f"A flow named {self.name!r} and defined at '{file}:{line_number}' "
                "conflicts with another flow. Consider specifying a unique `name` "
                "parameter in the flow definition:\n\n "
                "`@flow(name='my_unique_name', ...)`"
            )
        self.on_completion = on_completion
        self.on_failure = on_failure
        self.on_cancellation = on_cancellation
        self.on_crashed = on_crashed

    def with_options(
        self,
        *,
        name: str = None,
        version: str = None,
        retries: int = 0,
        retry_delay_seconds: Union[int, float] = 0,
        description: str = None,
        flow_run_name: Optional[Union[Callable[[], str], str]] = None,
        task_runner: Union[Type[BaseTaskRunner], BaseTaskRunner] = None,
        timeout_seconds: Union[int, float] = None,
        validate_parameters: bool = None,
        persist_result: Optional[bool] = NotSet,
        result_storage: Optional[ResultStorage] = NotSet,
        result_serializer: Optional[ResultSerializer] = NotSet,
        cache_result_in_memory: bool = None,
        log_prints: Optional[bool] = NotSet,
        on_completion: Optional[
            List[Callable[[schemas.core.Flow, FlowRun, State], None]]
        ] = None,
        on_failure: Optional[
            List[Callable[[schemas.core.Flow, FlowRun, State], None]]
        ] = None,
        on_cancellation: Optional[
            List[Callable[[schemas.core.Flow, FlowRun, State], None]]
        ] = None,
        on_crashed: Optional[
            List[Callable[[schemas.core.Flow, FlowRun, State], None]]
        ] = None,
    ):
        """
        Create a new flow from the current object, updating provided options.

        Args:
            name: A new name for the flow.
            version: A new version for the flow.
            description: A new description for the flow.
            flow_run_name: An optional name to distinguish runs of this flow; this name
                can be provided as a string template with the flow's parameters as variables,
                or a function that returns a string.
            task_runner: A new task runner for the flow.
            timeout_seconds: A new number of seconds to fail the flow after if still
                running.
            validate_parameters: A new value indicating if flow calls should validate
                given parameters.
            retries: A new number of times to retry on flow run failure.
            retry_delay_seconds: A new number of seconds to wait before retrying the
                flow after failure. This is only applicable if `retries` is nonzero.
            persist_result: A new option for enabling or disabling result persistence.
            result_storage: A new storage type to use for results.
            result_serializer: A new serializer to use for results.
            cache_result_in_memory: A new value indicating if the flow's result should
                be cached in memory.
            on_failure: A new list of callables to run when the flow enters a failed state.
            on_completion: A new list of callables to run when the flow enters a completed state.
            on_cancellation: A new list of callables to run when the flow enters a cancelling state.
            on_crashed: A new list of callables to run when the flow enters a crashed state.

        Returns:
            A new `Flow` instance.

        Examples:

            Create a new flow from an existing flow and update the name:

            >>> @flow(name="My flow")
            >>> def my_flow():
            >>>     return 1
            >>>
            >>> new_flow = my_flow.with_options(name="My new flow")

            Create a new flow from an existing flow, update the task runner, and call
            it without an intermediate variable:

            >>> from prefect.task_runners import SequentialTaskRunner
            >>>
            >>> @flow
            >>> def my_flow(x, y):
            >>>     return x + y
            >>>
            >>> state = my_flow.with_options(task_runner=SequentialTaskRunner)(1, 3)
            >>> assert state.result() == 4

        """
        return Flow(
            fn=self.fn,
            name=name or self.name,
            description=description or self.description,
            flow_run_name=flow_run_name,
            version=version or self.version,
            task_runner=task_runner or self.task_runner,
            retries=retries or self.retries,
            retry_delay_seconds=retry_delay_seconds or self.retry_delay_seconds,
            timeout_seconds=(
                timeout_seconds if timeout_seconds is not None else self.timeout_seconds
            ),
            validate_parameters=(
                validate_parameters
                if validate_parameters is not None
                else self.should_validate_parameters
            ),
            persist_result=(
                persist_result if persist_result is not NotSet else self.persist_result
            ),
            result_storage=(
                result_storage if result_storage is not NotSet else self.result_storage
            ),
            result_serializer=(
                result_serializer
                if result_serializer is not NotSet
                else self.result_serializer
            ),
            cache_result_in_memory=(
                cache_result_in_memory
                if cache_result_in_memory is not None
                else self.cache_result_in_memory
            ),
            log_prints=log_prints if log_prints is not NotSet else self.log_prints,
            on_completion=on_completion or self.on_completion,
            on_failure=on_failure or self.on_failure,
            on_cancellation=on_cancellation or self.on_cancellation,
            on_crashed=on_crashed or self.on_crashed,
        )

    def validate_parameters(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Validate parameters for compatibility with the flow by attempting to cast the inputs to the
        associated types specified by the function's type annotations.

        Returns:
            A new dict of parameters that have been cast to the appropriate types

        Raises:
            ParameterTypeError: if the provided parameters are not valid
        """
        validated_fn = ValidatedFunction(self.fn, config=None)
        args, kwargs = parameters_to_args_kwargs(self.fn, parameters)

        try:
            model = validated_fn.init_model_instance(*args, **kwargs)
        except pydantic.ValidationError as exc:
            # We capture the pydantic exception and raise our own because the pydantic
            # exception is not picklable when using a cythonized pydantic installation
            raise ParameterTypeError.from_validation_error(exc) from None

        # Get the updated parameter dict with cast values from the model
        cast_parameters = {
            k: v
            for k, v in model._iter()
            if k in model.__fields_set__ or model.__fields__[k].default_factory
        }
        return cast_parameters

    def serialize_parameters(self, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert parameters to a serializable form.

        Uses FastAPI's `jsonable_encoder` to convert to JSON compatible objects without
        converting everything directly to a string. This maintains basic types like
        integers during API roundtrips.
        """
        serialized_parameters = {}
        for key, value in parameters.items():
            try:
                serialized_parameters[key] = jsonable_encoder(value)
            except (TypeError, ValueError):
                logger.debug(
                    f"Parameter {key!r} for flow {self.name!r} is of unserializable "
                    f"type {type(value).__name__!r} and will not be stored "
                    "in the backend."
                )
                serialized_parameters[key] = f"<{type(value).__name__}>"
        return serialized_parameters

    @overload
    def __call__(
        self: "Flow[P, T]",
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> T:
        ...

    @overload
    def __call__(
        self: "Flow[P, T]",
        *args: P.args,
        return_state: Literal[True],
        **kwargs: P.kwargs,
    ) -> State[T]:
        ...

    def __call__(
        self,
        *args: "P.args",
        return_state: bool = False,
        wait_for: Optional[Iterable[PrefectFuture]] = None,
        **kwargs: "P.kwargs",
    ):
        """
        Run the flow and return its result.


        Flow parameter values must be serializable by Pydantic.

        If writing an async flow, this call must be awaited.

        This will create a new flow run in the API.

        Args:
            *args: Arguments to run the flow with.
            return_state: Return a Prefect State containing the result of the
                flow run.
            wait_for: Upstream task futures to wait for before starting the flow if called as a subflow
            **kwargs: Keyword arguments to run the flow with.

        Returns:
            If `return_state` is False, returns the result of the flow run.
            If `return_state` is True, returns the result of the flow run
                wrapped in a Prefect State which provides error handling.

        Examples:

            Define a flow

            >>> @flow
            >>> def my_flow(name):
            >>>     print(f"hello {name}")
            >>>     return f"goodbye {name}"

            Run a flow

            >>> my_flow("marvin")
            hello marvin
            "goodbye marvin"

            Run a flow with additional tags

            >>> from prefect import tags
            >>> with tags("db", "blue"):
            >>>     my_flow("foo")
        """
        from prefect.engine import enter_flow_run_engine_from_flow_call

        # Convert the call args/kwargs to a parameter dict
        parameters = get_call_parameters(self.fn, args, kwargs)

        return_type = "state" if return_state else "result"

        return enter_flow_run_engine_from_flow_call(
            self,
            parameters,
            wait_for=wait_for,
            return_type=return_type,
        )

    @overload
    def _run(self: "Flow[P, NoReturn]", *args: P.args, **kwargs: P.kwargs) -> State[T]:
        # `NoReturn` matches if a type can't be inferred for the function which stops a
        # sync function from matching the `Coroutine` overload
        ...

    @overload
    def _run(
        self: "Flow[P, Coroutine[Any, Any, T]]", *args: P.args, **kwargs: P.kwargs
    ) -> Awaitable[T]:
        ...

    @overload
    def _run(self: "Flow[P, T]", *args: P.args, **kwargs: P.kwargs) -> State[T]:
        ...

    def _run(
        self,
        *args: "P.args",
        wait_for: Optional[Iterable[PrefectFuture]] = None,
        **kwargs: "P.kwargs",
    ):
        """
        Run the flow and return its final state.

        Examples:

            Run a flow and get the returned result

            >>> state = my_flow._run("marvin")
            >>> state.result()
           "goodbye marvin"
        """
        from prefect.engine import enter_flow_run_engine_from_flow_call

        # Convert the call args/kwargs to a parameter dict
        parameters = get_call_parameters(self.fn, args, kwargs)

        return enter_flow_run_engine_from_flow_call(
            self,
            parameters,
            wait_for=wait_for,
            return_type="state",
        )


@overload
def flow(__fn: Callable[P, R]) -> Flow[P, R]:
    ...


@overload
def flow(
    *,
    name: Optional[str] = None,
    version: Optional[str] = None,
    flow_run_name: Optional[Union[Callable[[], str], str]] = None,
    retries: Optional[int] = None,
    retry_delay_seconds: Optional[Union[int, float]] = None,
    task_runner: BaseTaskRunner = ConcurrentTaskRunner,
    description: str = None,
    timeout_seconds: Union[int, float] = None,
    validate_parameters: bool = True,
    persist_result: Optional[bool] = None,
    result_storage: Optional[ResultStorage] = None,
    result_serializer: Optional[ResultSerializer] = None,
    cache_result_in_memory: bool = True,
    log_prints: Optional[bool] = None,
    on_completion: Optional[
        List[Callable[[schemas.core.Flow, FlowRun, State], None]]
    ] = None,
    on_failure: Optional[
        List[Callable[[schemas.core.Flow, FlowRun, State], None]]
    ] = None,
    on_cancellation: Optional[
        List[Callable[[schemas.core.Flow, FlowRun, State], None]]
    ] = None,
    on_crashed: Optional[
        List[Callable[[schemas.core.Flow, FlowRun, State], None]]
    ] = None,
) -> Callable[[Callable[P, R]], Flow[P, R]]:
    ...


def flow(
    __fn=None,
    *,
    name: Optional[str] = None,
    version: Optional[str] = None,
    flow_run_name: Optional[Union[Callable[[], str], str]] = None,
    retries: int = None,
    retry_delay_seconds: Union[int, float] = None,
    task_runner: BaseTaskRunner = ConcurrentTaskRunner,
    description: str = None,
    timeout_seconds: Union[int, float] = None,
    validate_parameters: bool = True,
    persist_result: Optional[bool] = None,
    result_storage: Optional[ResultStorage] = None,
    result_serializer: Optional[ResultSerializer] = None,
    cache_result_in_memory: bool = True,
    log_prints: Optional[bool] = None,
    on_completion: Optional[
        List[Callable[[schemas.core.Flow, FlowRun, State], None]]
    ] = None,
    on_failure: Optional[
        List[Callable[[schemas.core.Flow, FlowRun, State], None]]
    ] = None,
    on_cancellation: Optional[
        List[Callable[[schemas.core.Flow, FlowRun, State], None]]
    ] = None,
    on_crashed: Optional[
        List[Callable[[schemas.core.Flow, FlowRun, State], None]]
    ] = None,
):
    """
    Decorator to designate a function as a Prefect workflow.

    This decorator may be used for asynchronous or synchronous functions.

    Flow parameters must be serializable by Pydantic.

    Args:
        name: An optional name for the flow; if not provided, the name will be inferred
            from the given function.
        version: An optional version string for the flow; if not provided, we will
            attempt to create a version string as a hash of the file containing the
            wrapped function; if the file cannot be located, the version will be null.
        flow_run_name: An optional name to distinguish runs of this flow; this name can
            be provided as a string template with the flow's parameters as variables,
            or a function that returns a string.
        task_runner: An optional task runner to use for task execution within the flow; if
            not provided, a `ConcurrentTaskRunner` will be instantiated.
        description: An optional string description for the flow; if not provided, the
            description will be pulled from the docstring for the decorated function.
        timeout_seconds: An optional number of seconds indicating a maximum runtime for
            the flow. If the flow exceeds this runtime, it will be marked as failed.
            Flow execution may continue until the next task is called.
        validate_parameters: By default, parameters passed to flows are validated by
            Pydantic. This will check that input values conform to the annotated types
            on the function. Where possible, values will be coerced into the correct
            type; for example, if a parameter is defined as `x: int` and "5" is passed,
            it will be resolved to `5`. If set to `False`, no validation will be
            performed on flow parameters.
        retries: An optional number of times to retry on flow run failure.
        retry_delay_seconds: An optional number of seconds to wait before retrying the
            flow after failure. This is only applicable if `retries` is nonzero.
        persist_result: An optional toggle indicating whether the result of this flow
            should be persisted to result storage. Defaults to `None`, which indicates
            that Prefect should choose whether the result should be persisted depending on
            the features being used.
        result_storage: An optional block to use to perist the result of this flow.
            This value will be used as the default for any tasks in this flow.
            If not provided, the local file system will be used unless called as
            a subflow, at which point the default will be loaded from the parent flow.
        result_serializer: An optional serializer to use to serialize the result of this
            flow for persistence. This value will be used as the default for any tasks
            in this flow. If not provided, the value of `PREFECT_RESULTS_DEFAULT_SERIALIZER`
            will be used unless called as a subflow, at which point the default will be
            loaded from the parent flow.
        log_prints: If set, `print` statements in the flow will be redirected to the
            Prefect logger for the flow run. Defaults to `None`, which indicates that
            the value from the parent flow should be used. If this is a parent flow,
            the default is pulled from the `PREFECT_LOGGING_LOG_PRINTS` setting.
        on_completion: An optional list of functions to call when the flow run is
            completed. Each function should accept three arguments: the flow, the flow
            run, and the final state of the flow run.
        on_failure: An optional list of functions to call when the flow run fails. Each
            function should accept three arguments: the flow, the flow run, and the
            final state of the flow run.
        on_cancellation: An optional list of functions to call when the flow run is
            cancelled. These functions will be passed the flow, flow run, and final state.
        on_crashed: An optional list of functions to call when the flow run crashes. Each
            function should accept three arguments: the flow, the flow run, and the
            final state of the flow run.

    Returns:
        A callable `Flow` object which, when called, will run the flow and return its
        final state.

    Examples:
        Define a simple flow

        >>> from prefect import flow
        >>> @flow
        >>> def add(x, y):
        >>>     return x + y

        Define an async flow

        >>> @flow
        >>> async def add(x, y):
        >>>     return x + y

        Define a flow with a version and description

        >>> @flow(version="first-flow", description="This flow is empty!")
        >>> def my_flow():
        >>>     pass

        Define a flow with a custom name

        >>> @flow(name="The Ultimate Flow")
        >>> def my_flow():
        >>>     pass

        Define a flow that submits its tasks to dask

        >>> from prefect_dask.task_runners import DaskTaskRunner
        >>>
        >>> @flow(task_runner=DaskTaskRunner)
        >>> def my_flow():
        >>>     pass
    """
    if __fn:
        return cast(
            Flow[P, R],
            Flow(
                fn=__fn,
                name=name,
                version=version,
                flow_run_name=flow_run_name,
                task_runner=task_runner,
                description=description,
                timeout_seconds=timeout_seconds,
                validate_parameters=validate_parameters,
                retries=retries,
                retry_delay_seconds=retry_delay_seconds,
                persist_result=persist_result,
                result_storage=result_storage,
                result_serializer=result_serializer,
                cache_result_in_memory=cache_result_in_memory,
                log_prints=log_prints,
                on_completion=on_completion,
                on_failure=on_failure,
                on_cancellation=on_cancellation,
                on_crashed=on_crashed,
            ),
        )
    else:
        return cast(
            Callable[[Callable[P, R]], Flow[P, R]],
            partial(
                flow,
                name=name,
                version=version,
                flow_run_name=flow_run_name,
                task_runner=task_runner,
                description=description,
                timeout_seconds=timeout_seconds,
                validate_parameters=validate_parameters,
                retries=retries,
                retry_delay_seconds=retry_delay_seconds,
                persist_result=persist_result,
                result_storage=result_storage,
                result_serializer=result_serializer,
                cache_result_in_memory=cache_result_in_memory,
                log_prints=log_prints,
                on_completion=on_completion,
                on_failure=on_failure,
                on_cancellation=on_cancellation,
                on_crashed=on_crashed,
            ),
        )


def select_flow(
    flows: Iterable[Flow], flow_name: str = None, from_message: str = None
) -> Flow:
    """
    Select the only flow in an iterable or a flow specified by name.

    Returns
        A single flow object

    Raises:
        MissingFlowError: If no flows exist in the iterable
        MissingFlowError: If a flow name is provided and that flow does not exist
        UnspecifiedFlowError: If multiple flows exist but no flow name was provided
    """
    # Convert to flows by name
    flows = {f.name: f for f in flows}

    # Add a leading space if given, otherwise use an empty string
    from_message = (" " + from_message) if from_message else ""
    if not flows:
        raise MissingFlowError(f"No flows found{from_message}.")

    elif flow_name and flow_name not in flows:
        raise MissingFlowError(
            f"Flow {flow_name!r} not found{from_message}. "
            f"Found the following flows: {listrepr(flows.keys())}. "
            "Check to make sure that your flow function is decorated with `@flow`."
        )

    elif not flow_name and len(flows) > 1:
        raise UnspecifiedFlowError(
            (
                f"Found {len(flows)} flows{from_message}:"
                f" {listrepr(sorted(flows.keys()))}. Specify a flow name to select a"
                " flow."
            ),
        )

    if flow_name:
        return flows[flow_name]
    else:
        return list(flows.values())[0]


def load_flows_from_script(path: str) -> List[Flow]:
    """
    Load all flow objects from the given python script. All of the code in the file
    will be executed.

    Returns:
        A list of flows

    Raises:
        FlowScriptError: If an exception is encountered while running the script
    """
    return registry_from_script(path).get_instances(Flow)


def load_flow_from_script(path: str, flow_name: str = None) -> Flow:
    """
    Extract a flow object from a script by running all of the code in the file.

    If the script has multiple flows in it, a flow name must be provided to specify
    the flow to return.

    Args:
        path: A path to a Python script containing flows
        flow_name: An optional flow name to look for in the script

    Returns:
        The flow object from the script

    Raises:
        FlowScriptError: If an exception is encountered while running the script
        MissingFlowError: If no flows exist in the iterable
        MissingFlowError: If a flow name is provided and that flow does not exist
        UnspecifiedFlowError: If multiple flows exist but no flow name was provided
    """
    return select_flow(
        load_flows_from_script(path),
        flow_name=flow_name,
        from_message=f"in script '{path}'",
    )


def load_flow_from_entrypoint(entrypoint: str) -> Flow:
    """
    Extract a flow object from a script at an entrypoint by running all of the code in the file.

    Args:
        entrypoint: a string in the format `<path_to_script>:<flow_func_name>`

    Returns:
        The flow object from the script

    Raises:
        FlowScriptError: If an exception is encountered while running the script
        MissingFlowError: If the flow function specified in the entrypoint does not exist
    """
    with PrefectObjectRegistry(
        block_code_execution=True,
        capture_failures=True,
    ):
        path, func_name = entrypoint.split(":")
        try:
            flow = import_object(entrypoint)
        except AttributeError as exc:
            raise MissingFlowError(
                f"Flow function with name {func_name!r} not found in {path!r}. "
            ) from exc

        if not isinstance(flow, Flow):
            raise MissingFlowError(
                f"Function with name {func_name!r} is not a flow. Make sure that it is "
                "decorated with '@flow'."
            )

        return flow


def load_flow_from_text(script_contents: AnyStr, flow_name: str):
    """
    Load a flow from a text script.

    The script will be written to a temporary local file path so errors can refer
    to line numbers and contextual tracebacks can be provided.
    """
    with NamedTemporaryFile(
        mode="wt" if isinstance(script_contents, str) else "wb",
        prefix=f"flow-script-{flow_name}",
        suffix=".py",
        delete=False,
    ) as tmpfile:
        tmpfile.write(script_contents)
        tmpfile.flush()
    try:
        flow = load_flow_from_script(tmpfile.name, flow_name=flow_name)
    finally:
        # windows compat
        tmpfile.close()
        os.remove(tmpfile.name)
    return flow
