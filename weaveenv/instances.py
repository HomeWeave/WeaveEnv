import json
import os
from uuid import uuid4

from peewee import DoesNotExist

from weavelib.exceptions import ObjectNotFound
from weavelib.rpc import RPCServer, ServerAPI, ArgParameter

from .database import PluginsDatabase, PluginData
from .plugins import RunnablePlugin


def get_machine_id(base_path):
    full_path = os.path.join(base_path, ".config")
    try:
        with open(full_path) as config_file:
            return json.load(config_file)["machine_id"]
    except (ValueError, KeyError):
        raise
    except IOError:
        machine_id = "machine-id-" + str(uuid4())
        with open(full_path, "w") as config_file:
            json.dump({"machine_id": machine_id}, config_file)


def register_plugin(service, plugin):
    conn = service.get_connection()
    token = service.get_auth_token()

    # TODO: Find a find to get find_rpc(..) work for core system RPC too.
    rpc_info = find_rpc(service, None)
    client = RPCClient(conn, rpc_info, token)

    # TODO: Make InstalledPlugin, and RunnablePlugin have GitHub url in them.
    return client["register_plugin"](plugin.plugin_id(), plugin.name,
                                     plugin.src, _block=True)


def load_enabled_plugins(plugins, service, plugin_manager):
    result = []
    for plugin in plugins:
        if not plugin.is_enabled:
            continue

        path = os.path.join(plugin_manager.plugin_dir, plugin.app_id)
        if not os.path.isdir(path):
            continue

        # TODO: checks to ensure plugins are loadable and are not tampered with.

        # Register this plugin with ApplicationManager.
        auth_token = register_plugin(service, plugin)
        venv = plugin_manager.get_venv(plugin)
        result.append(RunnablePlugin(path, venv, plugin.name,
                                     plugin.description, auth_token))
    return result


class BaseWeaveEnvInstance(object):
    def start(self):
        raise NotImplementedError

    def list_plugins(self):
        raise NotImplementedError

    def activate(self, plugin_id):
        raise NotImplementedError

    def deactivate(self, plugin_id):
        raise NotImplementedError

    def install(self, plugin_url):
        raise NotImplementedError

    def uninstall(self, plugin_id):
        raise NotImplementedError


class LocalWeaveInstance(BaseWeaveEnvInstance):
    def __init__(self, service, instance_data, plugin_manager):
        self.instance_data = instance_data
        self.plugin_manager = plugin_manager
        self.rpc_server = RPCServer("WeaveEnv", "WeaveInstance Manager", [
            ServerAPI("list_plugins", "List plugins.", [], self.list_plugins),
            ServerAPI("activate_plugin", "Activate a plugin", [
                ArgParameter("plugin_id", "PluginID to activate", str),
            ], self.activate),
            ServerAPI("deactivate_plugin", "Deactivate a plugin", [
                ArgParameter("plugin_id", "PluginID to deactivate", str),
            ], self.deactivate),
            ServerAPI("install_plugin", "Install a plugin", [
                ArgParameter("plugin_url", "URL ending with .git.", str),
            ], self.install),
            ServerAPI("uninstall_plugin", "Uninstall a plugin", [
                ArgParameter("plugin_id", "PluginID to uninstall", str),
            ], self.uninstall),
        ], service)

    def activate(self, plugin_id):
        plugin_data = self.get_plugin_by_id(plugin_id)

    def get_plugin_by_id(self, plugin_id):
        try:
            return PluginData.get(PluginData.app_id == plugin_id)
        except DoesNotExist:
            raise ObjectNotFound(plugin_id)


class RemoteWeaveInstance(BaseWeaveEnvInstance):
    pass


class WeaveEnvInstanceManager(object):
    def __init__(self, base_path):
        self.plugin_dir = os.path.join(base_path, "plugins")
        self.venv_dir = os.path.join(base_path, "venv")
        self.db = PluginsDatabase(base_path, "weave.db")
        self.machine_id = get_machine_id(base_path)

    def get_plugin_dir(self):
        return self.plugin_dir

    def get_venv_dir(self):
        return self.venv_dir

    def get_database(self):
        return self.db
