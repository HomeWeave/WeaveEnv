import hashlib
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
from threading import RLock

import git
import virtualenv
from github3 import GitHub

from weavelib.exceptions import WeaveException, PluginLoadError
from weavelib.services import BasePlugin as BaseServicePlugin


logger = logging.getLogger(__name__)


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


def load_plugin_json(install_path):
    try:
        with open(os.path.join(install_path, "plugin.json")) as inp:
            plugin_info = json.load(inp)
    except IOError:
        raise PluginLoadError("Error opening plugin.json.")
    except ValueError:
        raise PluginLoadError("Error parsing plugin.json.")

    sys.path.append(install_path)
    try:
        fully_qualified = plugin_info["service"]
        if '.' not in fully_qualified:
            raise PluginLoadError("Bad 'service' specification in plugin.json.")
        mod, cls = plugin_info["service"].rsplit('.', 1)
        module = getattr(importlib.import_module(mod), cls)

        if not issubclass(module, BaseServicePlugin):
            raise PluginLoadError("Service must inherit BasePlugin.")
    except AttributeError:
        logger.warning("Bad service specification.", exc_info=True)
        raise PluginLoadError("Bad service specification in plugin.json")
    except ImportError:
        raise PluginLoadError("Failed to import dependencies.")
    except KeyError:
        raise PluginLoadError("Required field not found in plugin.json.")
    finally:
        sys.path.pop(-1)

    return {
        "deps": plugin_info.get("deps"),
        "package_path": plugin_info["service"],
        "config": plugin_info.get("config", {}),
        "start_timeout": plugin_info.get("start_timeout", 30),
        "service_cls": module,
    }


def list_github_plugins(organization='HomeWeave'):
    for repo in GitHub().organization(organization).repositories():
        contents = repo.directory_contents("/", return_as=dict)

        if "plugin.json" in contents:
            yield GitPlugin(repo.clone_url, repo.name, repo.description)


def url_to_plugin_id(url):
    return hashlib.md5(url.encode('utf-8')).hexdigest()


class VirtualEnvManager(object):
    def __init__(self, path):
        self.venv_home = path

    def install(self, requirements_file=None):
        if os.path.exists(self.venv_home):
            return True

        virtualenv.create_environment(self.venv_home, clear=True)

        if requirements_file:
            args = [os.path.join(self.venv_home, 'bin/python'), '-m', 'pip',
                    'install', '-r', requirements_file]
            try:
                subprocess.check_call(args)
            except subprocess.CalledProcessError:
                logger.exception("Unable to install requirements for %s.",
                                 self.venv_home)
                return False
        return True

    def is_installed(self):
        return os.path.exists(self.venv_home)

    def activate(self):
        script = os.path.join(self.venv_home, "bin", "activate_this.py")
        execute_file(script)

    def clean(self):
        shutil.rmtree(self.venv_home)


class BasePlugin(object):
    def __init__(self, src, name, description):
        self.src = src
        self.name = name
        self.description = description

    def plugin_id(self):
        return url_to_plugin_id(self.src)

    def install(self, dest_dir, venv):
        raise NotImplementedError

    def is_installed(self):
        return False

    def is_enabled(self):
        raise NotImplementedError

    def get_file(self, rel_path):
        return os.path.join(self.src, rel_path)

    def __hash__(self):
        return hash(self.plugin_id())

    def __eq__(self, other):
        return (isinstance(other, BasePlugin) and
                self.plugin_id() == other.plugin_id())


class InstalledPlugin(BasePlugin):
    def __init__(self, src, venv_manager, name, description):
        super().__init__(src, name, description)
        self.venv_manager = venv_manager

    def plugin_id(self):
        return os.path.basename(self.src)

    def is_installed(self):
        return os.path.isdir(self.src) and self.venv_manager.is_installed()

    def clean(self):
        if os.path.isdir(self.src):
            shutil.rmtree(self.src)
        self.venv_manager.clean()

    def get_plugin_dir(self):
        return self.src

    def get_venv_dir(self):
        return self.venv_manager.venv_home


class RunnablePlugin(InstalledPlugin):
    def __init__(self, src, venv_manager, name, description, auth_token):
        super().__init__(src, venv_manager, name, description)
        self.auth_token = auth_token

    def run(self):
        plugin_info = load_plugin_json(self.src)

        service_cls = plugin_info["service_cls"]
        start_timeout = plugin_info["start_timeout"]
        config = plugin_info["config"]

        service = service_cls(self.auth_token, config,
                              self.venv_manager.venv_home)

        if not run_plugin(service, timeout=start_timeout):
            raise WeaveException("Unable to start plugin.")

        logger.info("Started plugin: %s", plugin_info["name"])

        return RunningPlugin(self.src, self.venv_manager, self.name,
                             self.description, service)


class RunningPlugin(InstalledPlugin):
    def __init__(self, src, venv_manager, name, description, service):
        super().__init__(src, venv_manager, name, description)
        self.service = service

    def stop(self):
        stop_plugin(self.service)


class GitPlugin(BasePlugin):
    def __init__(self, src, name, description):
        super().__init__(src, name, description)
        self.clone_url = src

    def install(self, plugin_base_dir, venv):
        cloned_location = os.path.join(plugin_base_dir, self.plugin_id())

        # Clear the directory if already present.
        if os.path.isdir(cloned_location):
            shutil.rmtree(cloned_location)

        git.Repo.clone_from(self.clone_url, cloned_location)
        return InstalledPlugin(cloned_location, venv, self.name,
                               self.description)


class RemoteFilePlugin(BasePlugin):
    def install(self, dest_dir, venv):
        plugin_path = os.path.join(dest_dir, self.plugin_id())
        shutil.copytree(self.src, plugin_path)
        return InstalledPlugin(plugin_path, venv)


class PluginManager(object):
    def __init__(self, base_path):
        self.plugin_dir = os.path.join(base_path, "plugins")
        self.venv_dir = os.path.join(base_path, "venv")
        self.plugins = {}
        self.active_plugins = {}
        self.active_plugins_lock = RLock()

        os.makedirs(self.plugin_dir, exist_ok=True)

    def start(self, installed_plugins):
        plugins = set(installed_plugins) | set(list_github_plugins())
        self.plugins = {x.plugin_id(): x for x in plugins}

        # Start enabled plugins.
        for plugin in self.plugins.values():
            if isinstance(plugin, RunnablePlugin):
                self.activate(plugin, plugin.auth_token)

    def stop(self):
        for plugin in self.plugins.values():
            if isinstance(plugin, RunningPlugin):
                plugin.stop()

    def is_active(self, plugin):
        with self.active_plugins_lock:
            return plugin in self.active_plugins

    def activate(self, plugin):
        if self.is_active(plugin):
            return True

        if not isinstance(plugin, RunnablePlugin):
            raise TypeError("Expected a runnable plugin.")

        self.active_plugins[plugin.plugin_id()] = plugin.run()
        logger.info("Started plugin: %s", plugin.name)
        return True

    def deactivate(self, plugin):
        plugin_id = plugin.plugin_id()
        if not self.is_active(plugin_id):
            raise ValueError("Plugin is not active.")

        plugin = self.active_plugins[plugin_id]
        plugin.stop()
        logger.info("Stopped plugin: %s", plugin.name)
        return True

    def install(self, installable_plugin):
        venv = self.get_venv(installable_plugin)
        installed_plugin = None
        try:
            installed_plugin = installable_plugin.install(self.plugin_dir, venv)

            # Configure a new VirtualEnv.
            requirements_file = installed_plugin.get_file("requirements.txt")
            if not os.path.isfile(requirements_file):
                requirements_file = None
            if not venv.install(requirements_file=requirements_file):
                raise WeaveException("Unable to install virtualenv.")

            return installed_plugin
        except Exception:
            logger.exception("Installation of plugin failed. Rolling back.")
            if installed_plugin:
                self.uninstall(installed_plugin)
            return None

    def uninstall(self, installed_plugin):
        installed_plugin.clean()

    def list(self, params):
        return list(self.plugins.values())

    def get_venv(self, plugin):
        venv_path = os.path.join(self.venv_dir, plugin.plugin_id())
        return VirtualEnvManager(venv_path)
