import hashlib
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
from threading import RLock
from uuid import uuid4

import git
import virtualenv
from github3 import GitHub

from weavelib.exceptions import WeaveException, PluginLoadError, ObjectNotFound
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

    return {
        "deps": plugin_info.get("deps"),
        "package_path": plugin_info["service"],
        "config": plugin_info.get("config", {}),
        "start_timeout": plugin_info.get("start_timeout", 30),
        "service_name": cls,
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

    def info(self):
        return {
            "plugin_id": self.plugin_id(),
            "name": self.name,
            "description": self.description,
            "enabled": False,
            "installed": False,
            "active": False
        }


class InstalledPlugin(BasePlugin):
    def __init__(self, src, venv_manager, name, description, git_plugin):
        super().__init__(src, name, description)
        self.venv_manager = venv_manager
        self.git_plugin = git_plugin

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

    def info(self):
        res = self.git_plugin.info()
        res['installed'] = self.is_installed()
        return res


class RunnablePlugin(InstalledPlugin):
    def __init__(self, src, venv_manager, name, description, auth_token,
                 git_plugin):
        super().__init__(src, venv_manager, name, description, git_plugin)
        self.auth_token = auth_token

    def run(self):
        plugin_info = load_plugin_json(self.src, load_service=False)

        start_timeout = plugin_info["start_timeout"]

        service = BaseServicePlugin(auth_token=self.auth_token,
                                    venv_dir=self.venv_manager.venv_home,
                                    plugin_dir=self.src,
                                    started_token=str(uuid4()))

        if not run_plugin(service, timeout=start_timeout):
            raise WeaveException("Unable to start plugin.")

        logger.info("Started plugin: %s", plugin_info["service_name"])

        return RunningPlugin(self.src, self.venv_manager, self.name,
                             self.description, service, self.git_plugin)

    def info(self):
        res = super(RunnablePlugin, self).info()
        res['enabled'] = True
        return res


class RunningPlugin(InstalledPlugin):
    def __init__(self, src, venv_manager, name, description, service,
                 git_plugin):
        super().__init__(src, venv_manager, name, description, git_plugin)
        self.service = service

    def stop(self):
        stop_plugin(self.service)

    def info(self):
        res = super(RunningPlugin, self).info()
        res['enabled'] = True
        res['active'] = True
        return res



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
                               self.description, self)

    def info(self):
        res = super(GitPlugin, self).info()
        res['git_url'] = self.clone_url
        return res


class PluginManager(object):
    def __init__(self, base_path):
        self.plugin_dir = os.path.join(base_path, "plugins")
        self.venv_dir = os.path.join(base_path, "venv")
        self.github_plugins = {}
        self.plugins = {}
        self.active_plugins = {}
        self.active_plugins_lock = RLock()

        os.makedirs(self.plugin_dir, exist_ok=True)

    def start(self):
        self.github_plugins = {x.plugin_id(): x for x in list_github_plugins()}
        self.plugins = self.github_plugins.copy()

    def start_plugins(self, plugin_tokens):
        for db_plugin, token in plugin_tokens:
            obj = self.load_plugin(db_plugin, token)
            self.plugins[obj.plugin_id()] = obj

        # self.active_plugins has the messaging plugin. Called before this fn.
        self.plugins.update(self.active_plugins)

        # Start enabled plugins.
        for plugin in self.plugins.values():
            if isinstance(plugin, RunnablePlugin):
                self.activate(plugin)

    def load_plugin(self, db_plugin, token):
        path = os.path.join(self.plugin_dir, db_plugin.app_id)
        if not os.path.isdir(path):
            return None

        venv_path = os.path.join(self.venv_dir, db_plugin.app_id)
        if not os.path.isdir(venv_path):
            return None

        git_plugin = self.github_plugins[db_plugin.app_id]
        venv = VirtualEnvManager(venv_path)
        if db_plugin.enabled and token is not None:
            plugin = RunnablePlugin(path, venv, db_plugin.name,
                                    db_plugin.description, token, git_plugin)
        else:
            plugin = InstalledPlugin(path, venv, db_plugin.name,
                                     db_plugin.description, git_plugin)

        self.plugins[plugin.plugin_id()] = plugin
        return plugin


    def stop(self):
        for plugin in self.plugins.values():
            if isinstance(plugin, RunningPlugin):
                plugin.stop()

    def is_active(self, plugin_url):
        plugin = self.get_plugin_by_url(plugin_url)
        with self.active_plugins_lock:
            return plugin.plugin_id() in self.active_plugins

    def activate(self, plugin_url):
        if self.is_active(plugin_url):
            return True

        plugin = self.get_plugin_by_url(plugin_url)
        if not isinstance(plugin, RunnablePlugin):
            if isinstance(plugin, InstalledPlugin):
                raise PluginLoadError("Plugin is not enabled: " + plugin_url)
            else:
                raise PluginLoadError("Plugin is not installed: " + plugin_url)

        with self.active_plugins_lock:
            self.active_plugins[plugin.plugin_id()] = plugin.run()
        return True

    def deactivate(self, plugin_url):
        if not self.is_active(plugin_url):
            raise ValueError("Plugin is not active.")

        plugin_id = url_to_plugin_id(plugin_url)
        with self.active_plugins_lock:
            plugin = self.active_plugins.pop(plugin_id)
        plugin.stop()
        return True

    def install(self, plugin_url):
        installable_plugin = self.get_plugin_by_url(plugin_url)
        venv = VirtualEnvManager(os.path.join(self.venv_dir,
                                              installable_plugin.plugin_id()))
        installed_plugin = None
        try:
            installed_plugin = installable_plugin.install(self.plugin_dir, venv)

            # Configure a new VirtualEnv.
            requirements_file = installed_plugin.get_file("requirements.txt")
            if not os.path.isfile(requirements_file):
                requirements_file = None
            if not venv.install(requirements_file=requirements_file):
                raise WeaveException("Unable to install virtualenv.")

            self.plugins[installable_plugin.plugin_id()] = installed_plugin
            return True
        except Exception:
            logger.exception("Installation of plugin failed. Rolling back.")
            if installed_plugin:
                self.uninstall(installed_plugin)
                raise PluginLoadError("Installation failed.")
            return False

    def uninstall(self, plugin_url):
        if self.is_active(plugin_url):
            raise PluginLoadError("Must stop the plugin first.")

        installed_plugin =  self.get_plugin_by_url(plugin_url)
        installed_plugin.clean()
        self.plugins.pop(installable_plugin.plugin_id())
        return True

    def list(self):
        return list(self.plugins.values())

    def get_plugin_by_url(self, plugin_url):
        plugin_id = url_to_plugin_id(plugin_url)
        try:
            return self.plugins[plugin_id]
        except KeyError:
            raise ObjectNotFound(plugin_url)
