import abc
import asyncio
import enum
import functools
import json
import logging
import os
import sys
import threading
import time
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generator, List, Mapping, Optional, Tuple, Type, TYPE_CHECKING
from unittest.mock import Mock

import websockets
from deepdiff import Delta

import lightning_app
from lightning_app.core.constants import APP_SERVER_PORT, APP_STATE_MAX_SIZE_BYTES, SUPPORTED_PRIMITIVE_TYPES
from lightning_app.utilities.exceptions import LightningAppStateException

if TYPE_CHECKING:
    from lightning_app.core.app import LightningApp
    from lightning_app.core.flow import LightningFlow
    from lightning_app.utilities.types import Component


logger = logging.getLogger(__name__)


@dataclass
class StateEntry:
    """dataclass used to keep track the latest state shared through the app REST API."""

    app_state: Mapping = field(default_factory=dict)
    served_state: Mapping = field(default_factory=dict)
    session_id: Optional[str] = None


class StateStore(ABC):
    """Base class of State store that provides simple key, value store to keep track of app state, served app
    state."""

    @abstractmethod
    def __init__(self):
        pass

    @abstractmethod
    def add(self, k: str):
        """Creates a new empty state with input key 'k'."""
        pass

    @abstractmethod
    def remove(self, k: str):
        """Deletes a state with input key 'k'."""
        pass

    @abstractmethod
    def get_app_state(self, k: str) -> Mapping:
        """returns a stored appstate for an input key 'k'."""
        pass

    @abstractmethod
    def get_served_state(self, k: str) -> Mapping:
        """returns a last served app state for an input key 'k'."""
        pass

    @abstractmethod
    def get_served_session_id(self, k: str) -> str:
        """returns session id for state of a key 'k'."""
        pass

    @abstractmethod
    def set_app_state(self, k: str, v: Mapping):
        """sets the app state for state of a key 'k'."""
        pass

    @abstractmethod
    def set_served_state(self, k: str, v: Mapping):
        """sets the served state for state of a key 'k'."""
        pass

    @abstractmethod
    def set_served_session_id(self, k: str, v: str):
        """sets the session id for state of a key 'k'."""
        pass


class InMemoryStateStore(StateStore):
    """In memory simple store to keep track of state through the app REST API."""

    def __init__(self):
        self.store = {}
        self.counter = 0

    def add(self, k):
        self.store[k] = StateEntry()

    def remove(self, k):
        del self.store[k]

    def get_app_state(self, k):
        return self.store[k].app_state

    def get_served_state(self, k):
        return self.store[k].served_state

    def get_served_session_id(self, k):
        return self.store[k].session_id

    def set_app_state(self, k, v):
        state_size = sys.getsizeof(v)
        if state_size > APP_STATE_MAX_SIZE_BYTES:
            raise LightningAppStateException(
                f"App state size is {state_size} bytes, which is larger than the recommended size "
                f"of {APP_STATE_MAX_SIZE_BYTES}. Please investigate this."
            )
        self.store[k].app_state = deepcopy(v)
        self.counter += 1

    def set_served_state(self, k, v):
        self.store[k].served_state = deepcopy(v)

    def set_served_session_id(self, k, v):
        self.store[k].session_id = v


class DistributedMode(enum.Enum):
    SINGLEPROCESS = enum.auto()
    MULTIPROCESS = enum.auto()
    CONTAINER = enum.auto()
    GRID = enum.auto()


class _LightningAppRef:
    _app_instance: Optional["LightningApp"] = None

    @classmethod
    def connect(cls, app_instance: "LightningApp") -> None:
        cls._app_instance = app_instance

    @classmethod
    def get_current(cls) -> Optional["LightningApp"]:
        if cls._app_instance:
            return cls._app_instance


def affiliation(component: "Component") -> Tuple[str, ...]:
    """Returns the affiliation of a component."""
    if component.name in ("root", ""):
        return ()
    return tuple(component.name.split(".")[1:])


class AppStateType(str, enum.Enum):
    STREAMLIT = "STREAMLIT"
    DEFAULT = "DEFAULT"


class BaseStatePlugin(abc.ABC):
    def __init__(self):
        self.authorized = None

    @abc.abstractmethod
    def should_update_app(self, deep_diff):
        pass

    @abc.abstractmethod
    def get_context(self):
        pass

    @abc.abstractmethod
    def render_non_authorized(self):
        pass


class AppStatePlugin(BaseStatePlugin):
    def should_update_app(self, deep_diff):
        return deep_diff

    def get_context(self):
        return {"type": AppStateType.DEFAULT.value}

    def render_non_authorized(self):
        pass


def target_fn():
    from streamlit.server.server import Server

    async def update_fn():
        server = Server.get_current()
        sessions = list(server._session_info_by_id.values())
        url = "localhost:8080" if "LIGHTNING_APP_STATE_URL" in os.environ else f"localhost:{APP_SERVER_PORT}"
        ws_url = f"ws://{url}/api/v1/ws"
        last_updated = time.time()
        async with websockets.connect(ws_url) as websocket:
            while True:
                _ = await websocket.recv()
                while (time.time() - last_updated) < 1:
                    time.sleep(0.1)
                for session in sessions:
                    session = session.session
                    session.request_rerun(session._client_state)
                last_updated = time.time()

    if Server._singleton:
        asyncio.run(update_fn())


class StreamLitStatePlugin(BaseStatePlugin):
    def __init__(self):
        super().__init__()
        import streamlit as st

        if hasattr(st, "session_state") and "websocket_thread" not in st.session_state:
            thread = threading.Thread(target=target_fn)
            st.session_state.websocket_thread = thread
            thread.setDaemon(True)
            thread.start()

    def should_update_app(self, deep_diff):
        return deep_diff

    def get_context(self):
        return {"type": AppStateType.DEFAULT.value}

    def render_non_authorized(self):
        pass


# Adapted from
# https://github.com/PyTorchLightning/pytorch-lightning/blob/master/pytorch_lightning/utilities/model_helpers.py#L21
def is_overridden(method_name: str, instance: Optional[object] = None, parent: Optional[Type[object]] = None) -> bool:
    if instance is None:
        return False

    if parent is None:
        if isinstance(instance, lightning_app.LightningFlow):
            parent = lightning_app.LightningFlow
        elif isinstance(instance, lightning_app.LightningWork):
            parent = lightning_app.LightningWork
        if parent is None:
            raise ValueError("Expected a parent")

    instance_attr = getattr(instance, method_name, None)
    if instance_attr is None:
        return False
    # `Mock(wraps=...)` support
    if isinstance(instance_attr, Mock):
        # access the wrapped function
        instance_attr = instance_attr._mock_wraps
    if instance_attr is None:
        return False

    parent_attr = getattr(parent, method_name, None)
    if parent_attr is None:
        raise ValueError("The parent should define the method")

    return instance_attr.__code__ != parent_attr.__code__


def _is_json_serializable(x: Any) -> bool:
    """Test whether a variable can be encoded as json."""
    if type(x) in SUPPORTED_PRIMITIVE_TYPES:
        # shortcut for primitive types that are not containers
        return True
    try:
        json.dumps(x, cls=LightningJSONEncoder)
        return True
    except (TypeError, OverflowError):
        # OverflowError is raised if number is too large to encode
        return False


def _set_child_name(component: "Component", child: "Component", new_name: str) -> str:
    """Computes and sets the name of a child given the parent, and returns the name."""
    child_name = f"{component.name}.{new_name}"
    child._name = child_name

    # the name changed, so recursively update the names of the children of this child
    if isinstance(child, lightning_app.core.LightningFlow):
        for n, c in child.flows.items():
            _set_child_name(child, c, n)
        for n, w in child.named_works(recurse=False):
            _set_child_name(child, w, n)
        for n in child._structures:
            s = getattr(child, n)
            _set_child_name(child, s, n)
    if isinstance(child, lightning_app.structures.Dict):
        for n, c in child.items():
            _set_child_name(child, c, n)
    if isinstance(child, lightning_app.structures.List):
        for c in child:
            _set_child_name(child, c, c.name.split(".")[-1])

    return child_name


def _delta_to_appstate_delta(root: "LightningFlow", component: "Component", delta: Delta) -> Delta:
    delta_dict = delta.to_dict()
    for changed in delta_dict.values():
        for delta_key in changed.copy().keys():
            val = changed[delta_key]

            new_prefix = "root"
            for p, c in _walk_to_component(root, component):

                if isinstance(c, lightning_app.core.LightningWork):
                    new_prefix += "['works']"

                if isinstance(c, lightning_app.core.LightningFlow):
                    new_prefix += "['flows']"

                if isinstance(c, (lightning_app.structures.Dict, lightning_app.structures.List)):
                    new_prefix += "['structures']"

                c_n = c.name.split(".")[-1]
                new_prefix += f"['{c_n}']"

            delta_key_without_root = delta_key[4:]  # the first 4 chars are the word 'root', strip it
            new_key = new_prefix + delta_key_without_root
            changed[new_key] = val
            del changed[delta_key]

    return Delta(delta_dict)


def _walk_to_component(
    root: "LightningFlow",
    component: "Component",
) -> Generator[Tuple["Component", "Component"], None, None]:
    """Returns a generator that runs through the tree starting from the root down to the given component.

    At each node, yields parent and child as a tuple.
    """
    from lightning_app.structures import Dict, List

    name_parts = component.name.split(".")[1:]  # exclude 'root' from the name
    parent = root
    for n in name_parts:
        if isinstance(parent, (Dict, List)):
            child = parent[n] if isinstance(parent, Dict) else parent[int(n)]
        else:
            child = getattr(parent, n)
        yield parent, child
        parent = child


def _collect_child_process_pids(pid: int) -> List[int]:
    """Function to return the list of child process pid's of a process."""
    processes = os.popen("ps -ej | grep -i 'python' | grep -v 'grep' | awk '{ print $2,$3 }'").read()
    processes = [p.split(" ") for p in processes.split("\n")[:-1]]
    return [int(child) for child, parent in processes if parent == str(pid) and child != str(pid)]


def _print_to_logger_info(*args, **kwargs):
    # TODO Find a better way to re-direct print to loggers.
    lightning_app._logger.info(" ".join([str(v) for v in args]))


def convert_print_to_logger_info(func: Callable) -> Callable:
    """This function is used to transform any print into logger.info calls, so it gets tracked in the cloud."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        original_print = __builtins__["print"]
        __builtins__["print"] = _print_to_logger_info
        res = func(*args, **kwargs)
        __builtins__["print"] = original_print
        return res

    return wrapper


def pretty_state(state: Dict) -> Dict:
    """Utility to prettify the state by removing hidden attributes."""
    new_state = {}
    for k, v in state["vars"].items():
        if not k.startswith("_"):
            if "vars" not in new_state:
                new_state["vars"] = {}
            new_state["vars"][k] = v
    if "flows" in state:
        for k, v in state["flows"].items():
            if "flows" not in new_state:
                new_state["flows"] = {}
            new_state["flows"][k] = pretty_state(state["flows"][k])
    if "works" in state:
        for k, v in state["works"].items():
            if "works" not in new_state:
                new_state["works"] = {}
            new_state["works"][k] = pretty_state(state["works"][k])
    return new_state


class LightningJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if callable(getattr(obj, "__json__", None)):
            return obj.__json__()
        return json.JSONEncoder.default(self, obj)