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

    def __str__(self):
        return "<Plugin: {} from {}>".format(self.name, self.src)

    def info(self):
        return {
            "plugin_id": self.plugin_id(),
            "name": self.name,
            "description": self.description,
            "enabled": False,
            "installed": False,
            "active": False
        }


class RemotePlugin(BasePlugin):
    def __init__(self, src, name, description):
        super().__init__(src, name, description)
        self.remote_url = src

    def info(self):
        res = super().info()
        res['remote_url'] = self.remote_url
        return res


class GitPlugin(RemotePlugin):
    def install(self, plugin_base_dir, venv):
        cloned_location = os.path.join(plugin_base_dir, self.plugin_id())

        # Clear the directory if already present.
        if os.path.isdir(cloned_location):
            shutil.rmtree(cloned_location)

        git.Repo.clone_from(self.remote_url, cloned_location)
        return InstalledPlugin(cloned_location, venv, self.name,
                               self.description, self)


class InstalledPlugin(BasePlugin):
    def __init__(self, src, venv_manager, name, description, remote_plugin):
        super().__init__(src, name, description)
        self.venv_manager = venv_manager
        self.remote_plugin = remote_plugin

    def plugin_id(self):
        return os.path.basename(self.src)

    def is_installed(self):
        return os.path.isdir(self.src) and self.venv_manager.is_installed()

    def clean(self):
        if os.path.isdir(self.src):
            shutil.rmtree(self.src)
        self.venv_manager.clean()
        return self.remote_plugin

    def get_plugin_dir(self):
        return self.src

    def get_venv_dir(self):
        return self.venv_manager.venv_home

    def info(self):
        res = self.remote_plugin.info()
        res.update(super().info())
        res['installed'] = self.is_installed()
        return res

    def __str__(self):
        return str(self.remote_plugin)


class RunnablePlugin(InstalledPlugin):
    def __init__(self, src, venv_manager, name, description, auth_token,
                 installed_plugin):
        super().__init__(src, venv_manager, name, description,
                         installed_plugin.remote_plugin)
        self.installed_plugin = installed_plugin
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
                             self.description, service, self)

    def info(self):
        res = super().info()
        res['enabled'] = True
        return res


class RunningPlugin(RunnablePlugin):
    def __init__(self, src, venv_manager, name, description, service,
                 runnable_plugin):
        super().__init__(src, venv_manager, name, description,
                         runnable_plugin.auth_token,
                         runnable_plugin.installed_plugin)
        self.runnable_plugin = runnable_plugin
        self.service = service

    def stop(self):
        stop_plugin(self.service)
        return self.runnable_plugin

    def info(self):
        res = super(RunningPlugin, self).info()
        res['active'] = True
        return res


class PluginManager(object):
    def __init__(self, base_path, lister_fn=list_github_plugins):
        self.plugin_dir = os.path.join(base_path, "plugins")
        self.venv_dir = os.path.join(base_path, "venv")
        self.remote_plugins = {}
        self.plugins = {}
        self.lister_fn = lister_fn

        os.makedirs(self.plugin_dir, exist_ok=True)

    def start(self):
        self.remote_plugins = {x.plugin_id(): x for x in self.lister_fn()}
        self.plugins = self.remote_plugins.copy()

    def start_plugins(self, plugin_tokens):
        for db_plugin, token in plugin_tokens:
            obj = self.load_plugin(db_plugin, token)
            self.plugins[obj.plugin_id()] = obj

        # Start enabled plugins.
        for plugin in self.plugins.values():
            if isinstance(plugin, RunnablePlugin):
                logger.info("Activating: %s", str(plugin))
                self.activate(plugin.installed_plugin.remote_plugin.remote_url)

    def load_plugin(self, db_plugin, token):
        path = os.path.join(self.plugin_dir, db_plugin.app_id)
        if not os.path.isdir(path):
            raise PluginLoadError("Plugin directory not found.")

        venv_path = os.path.join(self.venv_dir, db_plugin.app_id)
        if not os.path.isdir(venv_path):
            raise PluginLoadError("VirtualEnv directory not found.")

        plugin = self.get_plugin_by_id(db_plugin.app_id)

        venv = VirtualEnvManager(venv_path)
        if not isinstance(plugin, InstalledPlugin):
            plugin = InstalledPlugin(path, venv, db_plugin.name,
                                     db_plugin.description, plugin)

        if db_plugin.enabled != bool((token or "").strip()):
            raise ValueError("Token passed in not consistent with Plugin.")

        if db_plugin.enabled:
            if isinstance(plugin, RunnablePlugin):
                # This apparently has already been loaded.
                return plugin

            plugin = RunnablePlugin(path, venv, db_plugin.name,
                                    db_plugin.description, token.strip(),
                                    plugin)
            self.plugins[plugin.plugin_id()] = plugin
            return plugin
        else:
            if type(plugin) == InstalledPlugin:
                return plugin

            if isinstance(plugin, RunningPlugin):
                raise PluginLoadError("Must stop the plugin first.")

            plugin = InstalledPlugin(path, venv, db_plugin.name,
                                     db_plugin.description,
                                     plugin.installed_plugin.remote_plugin)
            self.plugins[plugin.plugin_id()] = plugin
            return plugin

    def stop(self):
        for plugin in self.plugins.values():
            if isinstance(plugin, RunningPlugin):
                plugin.stop()

    def activate(self, plugin_url):
        plugin = self.get_plugin_by_url(plugin_url)
        if isinstance(plugin, RunningPlugin):
            return plugin

        if not isinstance(plugin, RunnablePlugin):
            if isinstance(plugin, InstalledPlugin):
                raise PluginLoadError("Plugin is not enabled: " + plugin_url)
            else:
                raise PluginLoadError("Plugin is not installed: " + plugin_url)

        plugin = plugin.run()
        self.plugins[plugin.plugin_id()] = plugin
        return plugin

    def deactivate(self, plugin_url):
        plugin = self.get_plugin_by_url(plugin_url)
        if not isinstance(plugin, RunningPlugin):
            return plugin

        plugin = plugin.stop()
        self.plugins[plugin.plugin_id()] = plugin
        return plugin

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
            return installed_plugin
        except Exception:
            logger.exception("Installation of plugin failed. Rolling back.")
            if installed_plugin:
                self.uninstall(installed_plugin)
            raise PluginLoadError("Installation failed.")

    def uninstall(self, plugin_url):
        installed_plugin = self.get_plugin_by_url(plugin_url)

        if isinstance(installed_plugin, RunningPlugin):
            raise PluginLoadError("Must stop the plugin first.")

        if isinstance(installed_plugin, RunnablePlugin):
            raise PluginLoadError("Must disable the plugin first.")

        if not isinstance(installed_plugin, InstalledPlugin):
            raise PluginLoadError("Plugin not installed.")

        remote_plugin = installed_plugin.clean()
        self.plugins[installed_plugin.plugin_id()] = remote_plugin
        return remote_plugin

    def list(self):
        return list(self.plugins.values())

    def get_plugin_by_url(self, plugin_url):
        plugin_id = url_to_plugin_id(plugin_url)
        return self.get_plugin_by_id(plugin_id)

    def get_plugin_by_id(self, plugin_id):
        try:
            return self.plugins[plugin_id]
        except KeyError:
            raise ObjectNotFound(plugin_url)
