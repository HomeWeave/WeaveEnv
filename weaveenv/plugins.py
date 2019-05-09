import hashlib
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys

import git
import virtualenv
from github3 import GitHub

from weavelib.exceptions import WeaveException


logger = logging.getLogger(__name__)


class PluginLoadError(WeaveException):
    pass


def execute_file(path):
    global_vars = {"__file__":  path}
    with open(path, 'rb') as pyfile:
        exec(compile(pyfile.read(), path, 'exec'), global_vars)


def get_plugin_id(url):
    return hashlib.md5(url.encode('utf-8')).hexdigest()


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
        return None
    except ValueError:
        raise PluginLoadError("Error opening plugin.json.")

    sys.path.append(install_path)
    try:
        fully_qualified = plugin_info["service"]
        if '.' not in fully_qualified:
            raise PluginLoadError("Bad 'service' specification in plugin.json.")
        mod, cls = plugin_info["service"].rsplit('.', 1)
        module = getattr(importlib.import_module(mod), cls)
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


def get_plugin_info(install_manager, execution_manager, plugin):
    obj = load_plugin_json(plugin.get_plugin_dir())
    obj["active"] = execution_manager.is_active(plugin)

    if isinstance(plugin, InstalledPlugin) and plugin.is_installed():
        obj["installed"] = True
        obj["install_path"] = plugin.get_plugin_dir()
    else:
        obj["installed"] = False

    return obj


def list_github_plugins(organization='HomeWeave'):
    for repo in GitHub().organization(organization).repositories():
        contents = repo.directory_contents("/", return_as=dict)

        if "plugin.json" in contents:
            yield GitPlugin(repo.clone_url, repo.name, repo.description)


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
        return get_plugin_id(self.src)

    def install(self, dest_dir, venv):
        raise NotImplementedError

    def is_installed(self):
        return False

    def is_enabled(self):
        raise NotImplementedError

    def get_file(self, rel_path):
        return os.path.join(self.src, rel_path)


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
        return InstalledPlugin(cloned_location, venv)


class RemoteFilePlugin(BasePlugin):
    def __init__(self, src, name, description):
        super(RemoteFilePlugin, self).__init__(src, name, description)

    def install(self, dest_dir, venv):
        plugin_path = os.path.join(dest_dir, self.plugin_id())
        shutil.copytree(self.src, plugin_path)
        return InstalledPlugin(plugin_path, venv)


class PluginInstallManager(object):
    def __init__(self, plugin_dir, venv_dir):
        self.plugin_dir = plugin_dir
        self.venv_dir = venv_dir
        os.makedirs(self.plugin_dir, exist_ok=True)

    def install(self, installable_plugin):
        venv_path = os.path.join(self.venv_dir, installable_plugin.plugin_id())
        venv = VirtualEnvManager(venv_path)
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


class PluginExecutionManager(object):
    def __init__(self, plugin_dir, venv_dir, database):
        self.plugin_dir = plugin_dir
        self.venv_dir = venv_dir
        self.database = database
        self.active_plugins = {}

    def is_enabled(self, plugin_id):
        try:
            return self.get_plugin_data(plugin_id).enabled
        except ValueError:
            return False

    def enable(self, plugin_id):
        plugin_data = self.get_plugin_data(plugin_id)

        plugin_data.enabled = True
        plugin_data.save()
        return True

    def disable(self, plugin_id):
        plugin_data = self.get_plugin_data(plugin_id)
        plugin_data.enabled = False
        plugin_data.save()
        return True

    def is_active(self, plugin_id):
        return plugin_id in self.active_plugins

    def activate(self, plugin_info):
        plugin_id = plugin_info["id"]

        if not self.is_enabled(plugin_id):
            raise ValueError("Plugin is not enabled")

        if self.is_active(plugin_id):
            return True

        venv_dir = os.path.join(self.venv_dir, plugin_id)
        plugin_data = self.get_plugin_data(plugin_id)
        service = plugin_info["cls"](plugin_data.app_secret_token, {}, venv_dir)

        # TODO: Read timeout & config (above) from plugin.json.
        if not run_plugin(service, timeout=30):
            raise Exception("Unable to start plugin.")

        logger.info("Started plugin: %s", plugin_info["name"])
        self.active_plugins[plugin_id] = service
        return True

    def deactivate(self, plugin_id):
        if not self.is_active(plugin_id):
            raise ValueError("Plugin is not active.")

        service = self.active_plugins[plugin_id]
        stop_plugin(service)
        # TODO: Get the name of the plugin.
        logger.info("Stopped plugin: %s", service)
        return True

    def get_plugin_data(self, plugin_id):
        return self.database.query(plugin_id)



class PluginManager(object):
    def __init__(self, base_path, plugins_db):
        plugin_dir = os.path.join(base_path, "plugins")
        venv_dir = os.path.join(base_path, "venv")
        self.database = plugins_db
        self.install_manager = PluginInstallManager(plugin_dir, venv_dir)
        self.execution_manager = PluginExecutionManager(plugin_dir, venv_dir,
                                                        self.database)
        self.plugins = {}

    def start(self):
        self.database.start()
        github = GithubRepositoryLister("HomeWeave")
        self.plugins = {}
        for repo in list_github_plugins():
            plugin_info = self.extract_plugin_info(repo)
            self.plugins[repo["id"]] = plugin_info

            try:
                self.database.query(repo["id"])
            except ValueError:
                self.database.insert(app_id=repo["id"])

    def get_registrations(self):
        return [
            ("GET", "", self.list),
            ("POST", "activate", self.activate),
            ("POST", "deactivate", self.deactivate),
            ("POST", "install", self.install),
            ("POST", "uninstall", self.uninstall),
        ]

    def list(self, params):
        res = [self.convert_plugin(v) for v in self.plugins.values()]
        return 200, res

    def activate(self, params):
        plugin_id = params["id"]
        plugin_info = self.plugins.get(plugin_id)
        if not plugin_info:
            return 404, {"error": "Not found."}

        try:
            self.execution_manager.enable(plugin_id)
            self.execution_manager.activate(plugin_id)
        except ValueError as e:
            return 400, {"error": str(e)}

        updated_plugin_info = get_plugin_info(self.install_manager,
                                              self.execution_manager, plugin_id)
        self.plugins[plugin_id] = updated_plugin_info
        return 200, self.convert_plugin(updated_plugin_info)

    def deactivate(self, params):
        plugin_id = params["id"]
        plugin_info = self.plugins.get(plugin_id)
        if not plugin_info:
            return 404, {"error": "Not found."}

        try:
            self.execution_manager.disable(plugin_id)
            self.execution_manager.deactivate(plugin_id)
        except ValueError as e:
            return 400, {"error": str(e)}

        updated_plugin_info = get_plugin_info(self.install_manager,
                                              self.execution_manager, plugin_id)
        self.plugins[plugin_id] = updated_plugin_info
        return 200, self.convert_plugin(updated_plugin_info)

        return 200, {}

    def install(self, params):
        plugin_id = params["id"]
        plugin_info = self.plugins.get(plugin_id)
        if not plugin_info:
            return 404, {"error": "Not found."}

        plugin = self.install_manager.install(plugin_info)
        if not plugin:
            return 400, {"error": "Failed to install library."}

        updated_plugin_info = get_plugin_info(self.install_manager,
                                              self.execution_manager, plugin_id)
        self.plugins[plugin_id] = updated_plugin_info
        return 200, self.convert_plugin(updated_plugin_info)

    def uninstall(self, params):
        plugin_id = params["id"]
        plugin_info = self.plugins.get(plugin_id)
        if not plugin_info:
            return 404, {"error": "Not found."}

        self.install_manager.uninstall(plugin_id)
        updated_plugin_info = get_plugin_info(self.install_manager,
                                              self.execution_manager, plugin_id)
        self.plugins[plugin_id] = updated_plugin_info
        return 200, self.convert_plugin(updated_plugin_info)

    def convert_plugin(self, plugin):
        fields = ["id", "name", "description", "url", "installed", "enabled",
                  "active"]
        res = {x: plugin[x] for x in fields}

        optional_fields = ["errors"]
        for opt_field in optional_fields:
            if plugin.get(opt_field):
                res[opt_field] = plugin[opt_field]

        return res
