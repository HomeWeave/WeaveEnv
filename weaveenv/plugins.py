import hashlib
import importlib
import json
import logging
import os
import shutil
import subprocess
import sys
from uuid import uuid4

import git
import virtualenv
from github3 import GitHub


logger = logging.getLogger(__name__)


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

    def activate(self):
        script = os.path.join(self.venv_home, "bin", "activate_this.py")
        execute_file(script)

    def clean(self):
        shutil.rmtree(self.venv_home)


class BasePlugin(object):
    def __init__(self, src):
        self.src = src
        self.appid = "plugin-token-" + str(uuid4())

    def unique_id(self):
        return get_plugin_id(self.src)

    def is_installed(self):
        raise NotImplementedError

    def is_enabled(self):
        raise NotImplementedError


class InstalledPlugin(BasePlugin):
    def __init__(self, src):
        super().__init__(src)

    def unique_id(self):
        return os.path.basename(self.src)

    def is_installed(self):
        return os.path.isdir(self.src)

    def clean(self):
        if os.path.isdir(self.src):
            shutil.rmtree(self.src)

    def get_plugin_dir(self):
        return self.src


class GitPlugin(BasePlugin):
    def __init__(self, src, cloned_location=None):
        super().__init__(src)
        self.clone_url = src
        self.cloned_location = cloned_location

    def unique_id(self):
        return get_plugin_id(self.clone_url)

    def clone(self, plugin_base_dir):
        self.cloned_location = os.path.join(plugin_base_dir, self.unique_id())

        # Clear the directory if already present.
        if os.path.isdir(self.cloned_location):
            shutil.rmtree(self.cloned_location)

        git.Repo.clone_from(self.clone_url, self.cloned_location)
        return InstalledPlugin(self.cloned_location)

    def is_installed(self):
        return False


class RemoteFilePlugin(BasePlugin):
    def __init__(self, src, dest):
        if src is None:
            src = open(os.path.join(dest, "source")).read().strip()
            dest = os.path.dirname(dest)
        super(RemoteFilePlugin, self).__init__(src, dest)

    def create(self):
        shutil.copytree(self.src, self.plugin_dir)
        with open(os.path.join(self.plugin_dir, "source"), "w") as f:
            f.write(self.src)


class PluginInstallManager(object):
    def __init__(self, plugin_dir, venv_dir):
        self.plugin_dir = plugin_dir
        self.venv_dir = venv_dir
        os.makedirs(self.plugin_dir, exist_ok=True)

    def is_installed(self, plugin_id):
        plugin_dir = os.path.join(self.plugin_dir, plugin_id)
        venv_dir = os.path.join(self.plugin_dir, plugin_id)
        return os.path.isdir(plugin_dir) and os.path.isdir(venv_dir)

    def is_enabled(self, plugin_id):
        return False

    def install(self, plugin_info):
        git_plugin = GitPlugin(plugin_info["url"])

        venv_path = os.path.join(self.venv_dir, plugin_info["id"])
        venv = VirtualEnvManager(venv_path)
        try:
            # Clone the Git Repo.
            plugin = git_plugin.clone(self.plugin_dir)

            # Configure a new VirtualEnv.
            requirements_file = os.path.join(plugin.get_plugin_dir(),
                                            "requirements.txt")
            if not os.path.isfile(requirements_file):
                requirements_file = None
            if not venv.install(requirements_file=requirements_file):
                raise Exception("Unable to install virtualenv.")

            return plugin
        except Exception as e:
            logger.exception("Installation of plugin failed. Rolling back.")
            self.uninstall(plugin_info["id"])
            return None

    def uninstall(self, plugin_id):
        InstalledPlugin(os.path.join(self.plugin_dir, plugin_id)).clean()
        VirtualEnvManager(os.path.join(self.venv_dir, plugin_id)).clean()

    def get_plugin_path(self, plugin_id):
        return os.path.join(self.plugin_dir, plugin_id)

    def venv_exists(self, plugin_id):
        venv_dir = os.path.join(self.venv_dir, plugin_id)
        return os.path.isdir(venv_dir)


class GithubRepositoryLister(object):
    def __init__(self, organization):
        self.organization = None #GitHub().organization(organization)

    def list_plugins(self):
        yield {
            'id': '02989b3581044833800b17b2367865e7',
            'name': 'PhilipsHue',
            'url': 'https://github.com/HomeWeave/PhilipsHue.git',
            'description': None
        }
        yield {
            'id': 'e9f8e89b91a190b54d7522baedebd11c',
            'name': 'Dashboard',
            'url': 'https://github.com/HomeWeave/Dashboard.git',
            'description': None
        }
        yield {
            'id': 'da2fbdfcbf684bc1fe08d91a93ef2cec',
            'name': 'Linux',
            'url': 'https://github.com/HomeWeave/Linux.git',
            'description': None
        }
        return
        for repo in self.organization.repositories():
            contents = repo.directory_contents("/", return_as=dict)
            plugin_id = get_plugin_id(repo.clone_url)

            if "plugin.json" in contents:
                yield {
                    "id": plugin_id,
                    "name": repo.name,
                    "url": repo.clone_url,
                    "description": repo.description,
                }


class PluginStateFilter(object):
    def __init__(self, install_manager):
        self.install_manager = install_manager

    def filter(self, obj):
        obj["installed"] = self.install_manager.is_installed(obj["id"])
        obj["enabled"] = self.install_manager.is_enabled(obj["id"])

        if obj["installed"]:
            obj["install_path"] = \
                self.install_manager.get_plugin_path(obj["id"])

        return obj


class PluginInfoFilter(object):
    def filter(self, obj):
        if not obj.get("installed"):
            return obj

        try:
            with open(os.path.join(obj["install_path"], "plugin.json")) as inp:
                plugin_info = json.load(inp)
        except IOError:
            logger.warning("Error opening plugin.json within %s", obj["name"])
            return obj
        except ValueError:
            logger.warning("Error parsing plugin.json within %s", obj["name"])
            return obj

        sys.path.append(obj["install_path"])
        try:
            fully_qualified = plugin_info["service"]
            if '.' not in fully_qualified:
                logger.warning("Bad 'service' specification in plugin.json.")
                return obj
            mod, cls = plugin_info["service"].rsplit('.', 1)
            module = getattr(importlib.import_module(mod), cls)
        except AttributeError:
            msg = "Possibly bad service specification in plugin.json"
            logger.warning(msg, exc_info=True)
            obj.setdefault("errors", []).append(msg)
            return obj
        except ImportError:
            msg = "Failed to import dependencies for " + obj["name"]
            obj.setdefault("errors", []).append(msg)
            logger.warning(msg, exc_info=True)
            return obj
        except KeyError:
            msg = "Required field not found in plugin.json for " + obj["name"]
            obj.setdefault("errors", []).append(msg)
            logger.warning(msg, exc_info=True)
            return obj
        finally:
            sys.path.pop(-1)

        obj.update({
            "deps": plugin_info.get("deps"),
            "package_path": plugin_info["service"],
            "config": plugin_info.get("config", {}),
            "start_timeout": plugin_info.get("start_timeout", 30),
            "service_cls": module,
        })
        return obj


class PluginManager(object):
    def __init__(self, base_path):
        plugin_dir = os.path.join(base_path, "plugins")
        venv_dir = os.path.join(base_path, "venv")
        self.install_manager = PluginInstallManager(plugin_dir, venv_dir)
        self.plugins = {}

    def start(self):
        github = GithubRepositoryLister("HomeWeave")
        self.plugins = {}
        for repo in github.list_plugins():
            plugin_info = self.extract_plugin_info(repo)
            self.plugins[repo["id"]] = plugin_info

    def get_registrations(self):
        return [
            ("GET", "", self.list),
            ("POST", "activate", self.activate),
            ("POST", "deactivate", self.deactivate),
            ("POST", "install", self.install),
            ("POST", "uninstall", self.uninstall),
        ]

    def list(self, params):
        res = {k: self.convert_plugin(v) for k, v in self.plugins.items()}
        return 200, res

    def activate(self, params):
        pass

    def deactivate(self, params):
        pass

    def install(self, params):
        plugin_id = params["id"]
        plugin_info = self.plugins.get(plugin_id)
        if not plugin_info:
            return 404, {"error": "Not found."}

        plugin = self.install_manager.install(plugin_info)
        if not plugin:
            return 400, {"error": "Failed to install library."}

        updated_plugin_info = self.extract_plugin_info(plugin_info)
        self.plugins[plugin_id] = updated_plugin_info
        return 200, self.convert_plugin(updated_plugin_info)

    def uninstall(self, params):
        plugin_id = params["id"]
        plugin_info = self.plugins.get(plugin_id)
        if not plugin_info:
            return 404, {"error": "Not found."}

        self.install_manager.uninstall(plugin_id)
        updated_plugin_info = self.extract_plugin_info(plugin_info)
        self.plugins[plugin_id] = updated_plugin_info
        return 200, self.convert_plugin(updated_plugin_info)

    def convert_plugin(self, plugin):
        fields = ["id", "name", "description", "url", "installed", "enabled"]
        res = {x: plugin[x] for x in fields}

        optional_fields = ["errors"]
        for opt_field in optional_fields:
            if plugin.get(opt_field):
                res[opt_field] = plugin[opt_field]

        return res

    def extract_plugin_info(self, plugin_info):
        filters = [
            PluginStateFilter(self.install_manager),
            PluginInfoFilter(),
        ]
        for filt in filters:
            plugin_info = filt.filter(plugin_info)
        return plugin_info
