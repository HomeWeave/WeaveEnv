import hashlib
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional
from uuid import uuid4

import git  # type: ignore
import virtualenv  # type: ignore
from github3 import GitHub  # type: ignore
from peewee import PeeweeException, IntegrityError, DoesNotExist

from weavelib.exceptions import WeaveException, PluginLoadError, ObjectNotFound
from weavelib.exceptions import InternalError
from weavelib.rpc import RPCClient, find_rpc
from weavelib.services import MessagingEnabled, BasePlugin as BaseServicePlugin

from weaveenv.database import WeaveEnvInstanceData, PluginData


logger = logging.getLogger(__name__)
MESSAGING_PLUGIN_URL = "https://github.com/HomeWeave/WeaveServer.git"
VALID_CLASSES = [
    "http",
    "dashboard",
    "datastore"
]



def execute_file(path):
    global_vars = {"__file__":  path}
    with open(path, 'rb') as pyfile:
        exec(compile(pyfile.read(), path, 'exec'), global_vars)


def run_plugin(service, timeout):
    service.service_start()
    if not service.wait_for_start(timeout=timeout):
        service.service_stop()
        return False
    return True


def stop_plugin(service):
    service.service_stop()


def load_plugin_json(install_path, load_service=True):
    try:
        with open(os.path.join(install_path, "plugin.json")) as inp:
            plugin_info = json.load(inp)
    except IOError:
        raise PluginLoadError("Error opening plugin.json.")
    except ValueError:
        raise PluginLoadError("Error parsing plugin.json.")

    try:
        fully_qualified = plugin_info["service"]
        if '.' not in fully_qualified:
            raise PluginLoadError("Bad 'service' specification in plugin.json.")
        mod, cls = plugin_info["service"].rsplit('.', 1)
        module = None

        if load_service:
            sys.path.append(install_path)
            try:
                module = getattr(importlib.import_module(mod), cls)
            finally:
                sys.path.pop(-1)

    except AttributeError:
        logger.warning("Bad service specification.", exc_info=True)
        raise PluginLoadError("Bad service specification in plugin.json")
    except ImportError:
        raise PluginLoadError("Failed to import dependencies.")
    except KeyError:
        raise PluginLoadError("Required field not found in plugin.json.")
    finally:
        sys.path.pop(-1)

    exported_rpc_classes = plugin_info.get("exported_rpc_classes", {})
    dependencies = plugin_info.get("required_rpc_classes", [])

    invalid_exported_rpcs = [x for x in exported_rpc_classes.keys()
                             if x not in VALID_CLASSES]
    if invalid_exported_rpcs:
        logger.warning("Invalid RPC class exported %s", invalid_exported_rpcs)
        raise PluginLoadError("Invalid rpc_class exported.")

    invalid_deps = [x for x in dependencies if x not in VALID_CLASSES]
    if invalid_deps:
        logger.warning("Invalid dependencies %s", invalid_deps)
        raise PluginLoadError("Invalid dependencies.")

    return {
        "deps": plugin_info.get("deps"),
        "package_path": plugin_info["service"],
        "config": plugin_info.get("config", {}),
        "start_timeout": plugin_info.get("start_timeout", 30),
        "service_name": cls,
        "service_cls": module,
        "exported_rpc_classes": exported_rpc_classes,
        "required_rpc_classes": dependencies,
    }


def list_github_plugins(organization='HomeWeave'):
    for repo in Github().organization(organization).repositories():
        contents = repo.directory_contents("/", return_as=dict)

        if "plugin.json" in contents:
            yield (repo.clone_url, repo.name, repo.description)


def url_to_plugin_id(url: str) -> str:
    return hashlib.md5(url.encode('utf-8')).hexdigest()


class VirtualEnvManager(object):
    def __init__(self, path: Path):
        self.venv_home = path

    def install(self, requirements_file: Path = None):
        if not self.is_installed():
            virtualenv.create_environment(str(self.venv_home), clear=True)

        if requirements_file and requirements_file.is_file():
            args = [str(self.venv_home / 'bin/python'), '-m', 'pip',
                    'install', '-r', str(requirements_file)]
            try:
                subprocess.check_call(args, cwd=str(requirements_file.parent))
            except subprocess.CalledProcessError:
                logger.exception("Unable to install requirements for %s.",
                                 str(self.venv_home))
                return False
        return True

    def is_installed(self):
        return self.venv_home.is_dir()

    def activate(self):
        execute_file(str(self.venv_home / "bin" / "activate_this.py"))

    def clean(self):
        try:
            shutil.rmtree(str(self.venv_home))
        except FileNotFoundError:
            # Ignore.
            pass

@dataclass
class PluginState:
    # Metadata
    name: str
    description: str
    remote_url: str

    # States
    installed_dir: Path = None
    db_plugin: PluginData = None
    active: bool = False                   # Is the plugin running?
    token: Optional[str] = None            # Messaging token.

    # Execution vars.
    venv: VirtualEnvManager = None
    service: BaseServicePlugin = None
    start_timeout: int = 30                # Timeout after which to kill start.
    exported_rpc_classes: Dict[str, str] = field(default_factory=dict)
    required_rpc_classes: List[str] = field(default_factory=list)

    app_manager_client: RPCClient = None

    @property
    def plugin_id(self):
        return url_to_plugin_id(self.remote_url)

    @property
    def enabled(self) -> bool:
        return bool(self.db_plugin and self.db_plugin.enabled)

    @property
    def installed(self) -> bool:
        return self.installed_dir.is_dir() and self.venv.is_installed()

    def info(self):
        return {
            "name": self.name,
            "description": self.description,
            "remote_url": self.remote_url,
            "id": self.plugin_id,
            "active": self.active,
            "enabled": self.enabled,
            "installed": self.installed,
        }


class StateHook:
    def load(self, plugin_state: PluginState) -> None:
        pass

    def before_enable(self, plugin_state: PluginState) -> None:
        pass

    def on_enable(self, plugin_state: PluginState) -> None:
        pass

    def after_enable(self, plugin_state: PluginState) -> None:
        pass

    def before_disable(self, plugin_state: PluginState) -> None:
        pass

    def on_disable(self, plugin_state: PluginState) -> None:
        pass

    def after_disable(self, plugin_state: PluginState) -> None:
        pass

    def before_install(self, plugin_state: PluginState) -> None:
        pass

    def on_install(self, plugin_state: PluginState) -> None:
        pass

    def after_install(self, plugin_state: PluginState) -> None:
        pass

    def before_uninstall(self, plugin_state: PluginState) -> None:
        pass

    def on_uninstall(self, plugin_state: PluginState) -> None:
        pass

    def after_uninstall(self, plugin_state: PluginState) -> None:
        pass

    def before_activate(self, plugin_state: PluginState) -> None:
        pass

    def on_activate(self, plugin_state: PluginState) -> None:
        pass

    def after_activate(self, plugin_state: PluginState) -> None:
        pass

    def before_deactivate(self, plugin_state: PluginState) -> None:
        pass

    def on_deactivate(self, plugin_state: PluginState) -> None:
        pass

    def after_deactivate(self, plugin_state: PluginState) -> None:
        pass

    def stop(self) -> None:
        pass


class MessagingRegistrationHook(StateHook):
    def __init__(self, service: MessagingEnabled):
        self.service = service
        self.client: RPCClient = None

    def load(self, plugin_state: PluginState):
        rpc_info = find_rpc(self.service, MESSAGING_PLUGIN_URL, "app_manager")
        self.client = RPCClient(self.service.get_connection(), rpc_info,
                                self.service.get_auth_token())
        self.client.start()

        plugin_state.app_manager_client = self.client

        if plugin_state.enabled:
            self.on_activate(plugin_state)

    def stop(self):
        self.client.stop()

    def on_activate(self, state: PluginState):
        state.token = self.client["register_plugin"](state.name,
                                                     state.remote_url,
                                                     _block=True)

    def on_deactivate(self, plugin_state: PluginState):
        self.client["unregister_plugin"](plugin_state.remote_url, _block=True)
        plugin_state.token = None


class VirtualEnvHook(StateHook):
    def __init__(self, base_dir: Path):
        self.base_path = base_dir / "venv"

    def load(self, plugin_state: PluginState):
        venv_dir = self.base_path / plugin_state.plugin_id
        plugin_state.venv = VirtualEnvManager(venv_dir)

    def on_install(self, plugin_state: PluginState):
        requirements_file = plugin_state.installed_dir / "requirements.txt"
        plugin_state.venv.install(requirements_file=requirements_file)

    def after_install(self, plugin_state: PluginState):
        if not plugin_state.venv.is_installed():
            raise InternalError("VirtualEnv directory not found.")

    def on_uninstall(self, plugin_state: PluginState):
        plugin_state.venv.clean()

    def before_enable(self, plugin_state: PluginState):
        if not plugin_state.venv.is_installed():
            raise PluginLoadError("VirtualEnv not installed.")


class PluginFilesStateHook(StateHook):
    def __init__(self, base_dir: Path):
        self.base_path = base_dir / "plugins"

    def load(self, plugin_state: PluginState):
        plugin_state.installed_dir = self.base_path / plugin_state.plugin_id

    def on_install(self, plugin_state: PluginState):
        # Clear the directory if already present.
        if plugin_state.installed_dir.is_dir():
            shutil.rmtree(str(plugin_state.installed_dir))

        git.Repo.clone_from(plugin_state.remote_url,
                            str(plugin_state.installed_dir))

    def after_install(self, plugin_state: PluginState):
        if not plugin_state.installed_dir.is_dir():
            raise InternalError("Unable to install plugin.")

    def on_uninstall(self, plugin_state: PluginState):
        if plugin_state.installed_dir.is_dir():
            shutil.rmtree(str(plugin_state.installed_dir))

    def before_enable(self, plugin_state: PluginState):
        if not plugin_state.installed_dir.is_dir():
            raise PluginLoadError("Plugin not installed.")

    def before_activate(self, plugin_state: PluginState):
        self.before_enable(plugin_state)


class PluginJsonHook(StateHook):
    def before_activate(self, plugin_state: PluginState):
        res = load_plugin_json(plugin_state.installed_dir, load_service=False)
        plugin_state.start_timeout = res["start_timeout"]
        plugin_state.exported_rpc_classes = res["exported_rpc_classes"]
        plugin_state.required_rpc_classes = res["required_rpc_classes"]


class PluginDBHook(StateHook):
    def __init__(self, instance: WeaveEnvInstanceData):
        self.instance = instance

    def load(self, plugin_state: PluginState):
        try:
            p = PluginData.get(PluginData.machine == self.instance,
                               PluginData.app_url == plugin_state.remote_url)
        except DoesNotExist:
            return

        plugin_state.db_plugin = p

    def on_install(self, plugin_state: PluginState):
        params = {
            "app_url": plugin_state.remote_url,
            "name": plugin_state.name,
            "description": plugin_state.description,
            "machine": self.instance
        }
        plugin_state.db_plugin = PluginData(**params)
        try:
            plugin_state.db_plugin.save(force_insert=True)
        except IntegrityError:
            pass

    def on_uninstall(self, plugin_state: PluginState):
        if not plugin_state.db_plugin:
            return

        try:
            plugin_state.db_plugin.delete_instance()
        except DoesNotExist:
            # Silently ignore.
            pass

    def on_enable(self, plugin_state: PluginState):
        plugin_state.db_plugin.enabled = True
        plugin_state.db_plugin.save()

    def on_disable(self, plugin_state: PluginState):
        plugin_state.db_plugin.enabled = False
        plugin_state.db_plugin.save()


class PluginExecutionStateHook(StateHook):
    def load(self, plugin_state: PluginState):
        if not plugin_state.service:
            if plugin_state.db_plugin and plugin_state.db_plugin.enabled:
                self.before_activate(plugin_state)
                self.on_activate(plugin_state)
                self.after_activate(plugin_state)

    def before_enable(self, plugin_state: PluginState):
        if not plugin_state.installed:
            raise PluginLoadError("Plugin is not installed.")

    def before_activate(self, plugin_state: PluginState):
        self.before_enable(plugin_state)
        if not plugin_state.enabled:
            raise PluginLoadError("Plugin is not enabled.")

    def on_activate(self, plugin_state: PluginState):
        if not plugin_state.token or not plugin_state.token.strip():
            raise InternalError("Not registered with messaging server.")

        service = BaseServicePlugin(auth_token=plugin_state.token,
                                    venv_dir=plugin_state.venv.venv_home,
                                    plugin_dir=str(plugin_state.installed_dir),
                                    started_token=str(uuid4()))

        if not run_plugin(service, plugin_state.start_timeout):
            raise PluginLoadError("Unable to start the plugin.")

        plugin_state.service = service

    def after_activate(self, plugin_state: PluginState):
        plugin_state.active = True

    def on_deactivate(self, plugin_state: PluginState):
        stop_plugin(plugin_state.service)
        plugin_state.active = False

    def before_disable(self, plugin_state: PluginState):
        if plugin_state.active:
            raise PluginLoadError("Plugin must be stopped first.")

    def before_uninstall(self, plugin_state: PluginState):
        self.before_disable(plugin_state)

        if plugin_state.enabled:
            raise PluginLoadError("Must disable the plugin first.")


class PluginManager(object):
    def __init__(self, base_path: Path, instance_data: WeaveEnvInstanceData,
                 service: MessagingEnabled, lister_fn=list_github_plugins):
        self.plugin_states: Dict[str, PluginState] = {}
        self.lister_fn = lister_fn
        self.instance_data = instance_data
        self.base_path = base_path

        # Hooks
        self.messaging_hook = MessagingRegistrationHook(service)
        self.venv_hook = VirtualEnvHook(base_path)
        self.plugin_fs_hook = PluginFilesStateHook(base_path)
        self.plugin_json_hook = PluginJsonHook()
        self.plugin_db_hook = PluginDBHook(instance_data)
        self.plugin_exec_hook = PluginExecutionStateHook()

        self.hooks = [
            self.venv_hook,
            self.plugin_fs_hook,
            self.plugin_db_hook,
            self.plugin_json_hook,
            self.messaging_hook,
            self.plugin_exec_hook,
        ]

    def start(self):
        plugins = [PluginState(name=x[1],  remote_url=x[0], description=x[2])
                   for x in self.lister_fn()]
        self.plugin_states = {x.plugin_id: x for x in plugins}

        for plugin_state in self.plugin_states.values():
            for hook in self.hooks:
                hook.load(plugin_state)

    def stop(self):
        for plugin in self.plugin_states.values():
            for hook in reversed(self.hooks):
                hook.before_deactivate(plugin)

        for plugin in self.plugin_states.values():
            for hook in reversed(self.hooks):
                hook.after_deactivate(plugin)

        for hook in self.hooks:
            hook.stop()

    def activate(self, plugin_url):
        plugin = self.get_plugin_by_url(plugin_url)
        for hook in self.hooks:
            hook.before_activate(plugin)

        for hook in self.hooks:
            hook.on_activate(plugin)

        for hook in self.hooks:
            hook.after_activate(plugin)

        return plugin.info()

    def deactivate(self, plugin_url):
        plugin = self.get_plugin_by_url(plugin_url)
        for hook in reversed(self.hooks):
            hook.before_deactivate(plugin)

        for hook in reversed(self.hooks):
            hook.on_deactivate(plugin)

        for hook in reversed(self.hooks):
            hook.after_deactivate(plugin)

        return plugin.info()

    def enable(self, plugin_url):
        plugin = self.get_plugin_by_url(plugin_url)
        for hook in self.hooks:
            hook.before_enable(plugin)

        for hook in self.hooks:
            hook.on_enable(plugin)

        for hook in self.hooks:
            hook.after_enable(plugin)

        return plugin.info()

    def disable(self, plugin_url):
        plugin = self.get_plugin_by_url(plugin_url)
        for hook in reversed(self.hooks):
            hook.before_disable(plugin)

        for hook in reversed(self.hooks):
            hook.on_disable(plugin)

        for hook in reversed(self.hooks):
            hook.after_disable(plugin)

        return plugin.info()

    def install(self, plugin_url):
        plugin = self.get_plugin_by_url(plugin_url)
        for hook in self.hooks:
            hook.before_install(plugin)

        for hook in self.hooks:
            hook.on_install(plugin)

        for hook in self.hooks:
            hook.after_install(plugin)

        return plugin.info()

    def uninstall(self, plugin_url):
        plugin = self.get_plugin_by_url(plugin_url)
        for hook in reversed(self.hooks):
            hook.before_uninstall(plugin)

        for hook in reversed(self.hooks):
            hook.on_uninstall(plugin)

        for hook in reversed(self.hooks):
            hook.after_uninstall(plugin)

        return plugin.info()

    def list(self):
        return [x.info() for x in self.plugin_states.values()]

    def plugin_state(self, plugin_url):
        return self.get_plugin_by_url(plugin_url).info()

    def get_plugin_by_url(self, plugin_url):
        plugin_id = url_to_plugin_id(plugin_url)
        try:
            return self.plugin_states[plugin_id]
        except KeyError:
            raise ObjectNotFound(plugin_id)
