import os
from threading import Event

from peewee import DoesNotExist

from weavelib.exceptions import ObjectNotFound
from weavelib.rpc import RPCServer, ServerAPI, ArgParameter, RPCClient
from weavelib.rpc import find_rpc

from .database import PluginData
from .plugins import RunnablePlugin, InstalledPlugin, url_to_plugin_id


def register_plugin(service, plugin):
    conn = service.get_connection()
    token = service.get_auth_token()

    # TODO: Find a find to get find_rpc(..) work for core system RPC too.
    rpc_info = find_rpc(service, None)
    client = RPCClient(conn, rpc_info, token)

    # TODO: Make InstalledPlugin, and RunnablePlugin have GitHub url in them.
    return client["register_plugin"](plugin.plugin_id(), plugin.name,
                                     plugin.src, _block=True)


def load_installed_plugins(plugins, service, plugin_manager):
    result = []
    for plugin in plugins:
        path = os.path.join(plugin_manager.plugin_dir, plugin.app_id)
        if not os.path.isdir(path):
            continue

        # TODO: checks to ensure plugins are loadable and are not tampered with.

        # Register this plugin with ApplicationManager.
        if plugin.is_enabled:
            auth_token = register_plugin(service, plugin)
            venv = plugin_manager.get_venv(plugin)
            result.append(RunnablePlugin(path, venv, plugin.name,
                                         plugin.description, auth_token))
        else:
            venv = plugin_manager.get_venv(plugin)
            result.append(InstalledPlugin(path, venv, plugin.name,
                                          plugin.description))

    return result


def get_plugin_by_id(self, plugin_id):
    try:
        return PluginData.get(PluginData.app_id == plugin_id)
    except DoesNotExist:
        raise ObjectNotFound(plugin_id)


def get_plugin_by_url(self, url):
    return get_plugin_by_id(url_to_plugin_id(url))


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
        self.stopped = Event()

    def start(self):
        # Insert basic data into the DB such as command-line access Data and
        # current machine data.

        installed_plugins = load_installed_plugins(self.instance_data.plugins,
                                                   self.service,
                                                   self.plugin_manager)
        self.plugin_manager.start(installed_plugins)
        self.rpc_server.start()

    def stop(self):
        self.rpc_server.stop()
        self.plugin_manager.stop()
        self.stopped.set()

    def wait(self):
        self.stopped.wait()

    def activate(self, plugin_id):
        pass
