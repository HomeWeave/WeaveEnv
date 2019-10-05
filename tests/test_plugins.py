import json
import os
import shutil
import subprocess
import textwrap
from glob import glob

import pytest

from weavelib.exceptions import PluginLoadError
from weavelib.services import BasePlugin

from weaveenv.database import PluginData
from weaveenv.plugins import load_plugin_json, VirtualEnvManager, PluginManager
from weaveenv.plugins import InstalledPlugin, RemotePlugin, RunnablePlugin
from weaveenv.plugins import url_to_plugin_id


class TestPluginLoadJson(object):
    @pytest.fixture(autouse=True)
    def setup(self, tmpdir):
        self.tmpdir = tmpdir.strpath
        self.plugin_dir = os.path.join(self.tmpdir, "plugin")
        os.makedirs(self.plugin_dir)

    def write_content(self, path, contents):
        with open(os.path.join(self.plugin_dir, path), "w") as out:
            out.write(contents)

    def test_no_plugin_json(self):
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Error opening plugin.json."

    def test_invalid_json(self):
        self.write_content("plugin.json", "hello word.")

        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Error parsing plugin.json."

    def test_no_service_field(self):
        self.write_content("plugin.json", '{"hello": "world"}')
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Required field not found in plugin.json."

    def test_bad_service_specification(self):
        self.write_content("plugin.json", '{"service": "hello world"}')
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Bad 'service' specification in plugin.json."

    def test_failing_import(self):
        self.write_content("plugin.json", '{"service": "a.b.c"}')
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Failed to import dependencies."

    def test_failing_import2(self):
        self.write_content("plugin.json", '{"service": "weaveenv.plugins.tmp"}')
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra ==  "Bad service specification in plugin.json"

    def test_good_plugin_load(self):
        spec = {
            "service": "plugin.main.TestPlugin",
            "config": {"hello": "world"},
            "start_timeout": 10,
            "deps": [1, 2, 4]
        }
        main_py = """
        from weavelib.services import BasePlugin

        class TestPlugin(BasePlugin):
            pass
        """
        package_dir = os.path.join(self.plugin_dir, "plugin")
        main_py_path = os.path.join(self.plugin_dir, "plugin", "main.py")

        os.makedirs(self.plugin_dir + "/plugin")
        self.write_content("plugin.json", json.dumps(spec))
        self.write_content(main_py_path, textwrap.dedent(main_py))

        expected = {
            "deps": [1, 2, 4],
            "package_path": "plugin.main.TestPlugin",
            "config": {"hello": "world"},
            "start_timeout": 10,
            "service_name": "TestPlugin"
        }

        plugin_info = load_plugin_json(self.plugin_dir)
        plugin_class = plugin_info.pop("service_cls")

        assert expected == plugin_info
        assert plugin_class.__name__ == "TestPlugin"


class TestVirtualEnvManager(object):
    @pytest.fixture(autouse=True)
    def setup(self, tmpdir):
        self.tmpdir = tmpdir.strpath
        self.venv_dir = os.path.join(self.tmpdir, "venv")
        self.tmp_dir = os.path.join(self.tmpdir, "scratch")
        os.makedirs(self.tmp_dir)

    def teardown(self):
        shutil.rmtree(self.tmpdir)

    def write_content(self, path, contents):
        with open(os.path.join(self.tmp_dir, path), "w") as out:
            out.write(contents)

    def test_create_virtual_env(self):
        venv = VirtualEnvManager(self.venv_dir)

        assert venv.install()
        assert venv.install()

    def test_create_virtual_env_with_bad_requirements(self):
        self.write_content("requirements.txt", "skdjdlss_slkdjs")

        venv = VirtualEnvManager(self.venv_dir)

        assert not venv.install(os.path.join(self.tmp_dir, "requirements.txt"))

    def test_create_virtual_env_with_requirements(self):
        self.write_content("requirements.txt", "bottle")

        venv = VirtualEnvManager(self.venv_dir)

        assert venv.install(os.path.join(self.tmp_dir, "requirements.txt"))

        python = os.path.join(self.venv_dir, "bin/python")
        command = [python, "-c", "import bottle"]
        subprocess.check_call(command)

        assert venv.is_installed()

        venv.clean()

        assert not venv.is_installed()


class FileSystemPlugin(RemotePlugin):
    def install(self, plugin_base_dir, venv):
        target = os.path.join(plugin_base_dir, self.plugin_id())
        shutil.copytree(self.remote_url, target)
        return InstalledPlugin(target, venv, self.name, self.description, self)


class TestPluginLifecycle(object):
    def get_test_plugin_id(self, plugin_dir):
        return url_to_plugin_id(self.get_test_plugin_path(plugin_dir))

    def get_test_plugin_path(self, plugin_dir):
        testdata = os.path.join(os.path.dirname(__file__), "testdata")
        return os.path.join(testdata, plugin_dir)

    def get_test_plugin(self, dir_name):
        path = self.get_test_plugin_path(dir_name)
        return FileSystemPlugin(path, dir_name, "description")

    def list_plugins(self):
        pattern = os.path.join(os.path.dirname(__file__), "testdata/*/")
        plugin_dirs = [os.path.basename(x.rstrip('/')) for x in glob(pattern)]
        return [self.get_test_plugin(x) for x in plugin_dirs]

    @pytest.fixture(autouse=True)
    def setup(self, tmpdir):
        self.base_dir = tmpdir.strpath

    def teardown(self):
        shutil.rmtree(self.base_dir)

    def test_plugin_listing(self):
        pm = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm.start()

        expected = [
            {
                "name": "plugin1",
                "description": "description",
                "plugin_id": self.get_test_plugin_id('plugin1'),
                "enabled": False,
                "installed": False,
                "active": False,
                "remote_url": self.get_test_plugin_path('plugin1'),
            }
        ]

        assert [x.info() for x in pm.list()] == expected

    def test_plugin_install(self):
        pm = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm.start()
        plugin = pm.install(self.get_test_plugin_path('plugin1'))

        expected = {
            "name": "plugin1",
            "description": "description",
            "plugin_id": self.get_test_plugin_id('plugin1'),
            "enabled": False,
            "installed": True,
            "active": False,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }

        assert isinstance(plugin, InstalledPlugin)
        assert plugin.info() == expected

    def test_plugin_double_install(self):
        pm = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm.start()
        plugin = pm.install(self.get_test_plugin_path('plugin1'))

        expected = {
            "name": "plugin1",
            "description": "description",
            "plugin_id": self.get_test_plugin_id('plugin1'),
            "enabled": False,
            "installed": True,
            "active": False,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }

        assert isinstance(plugin, InstalledPlugin)

        plugin_new = pm.install(self.get_test_plugin_path('plugin1'))
        assert plugin_new is plugin

    def test_plugin_uninstall(self):
        pm = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm.start()
        plugin = pm.install(self.get_test_plugin_path('plugin1'))
        plugin = pm.uninstall(self.get_test_plugin_path('plugin1'))
        expected = {
            "name": "plugin1",
            "description": "description",
            "plugin_id": self.get_test_plugin_id('plugin1'),
            "enabled": False,
            "installed": False,
            "active": False,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }

        assert isinstance(plugin, RemotePlugin)
        assert plugin.info() == expected

    def test_plugin_bad_uninstall(self):
        pm = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm.start()
        with pytest.raises(PluginLoadError, match="Plugin not installed."):
            pm.uninstall(self.get_test_plugin_path('plugin1'))

    def test_uninstall_enabled_plugin(self):
        pm = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm.start()
        plugin_url = self.get_test_plugin_path('plugin1')
        plugin = pm.install(plugin_url)

        db_plugin = PluginData(name="plugin1", description="description",
                               app_url=plugin_url, enabled=True)
        plugin = pm.load_plugin(db_plugin, "token")

        with pytest.raises(PluginLoadError, match="Must disable the plugin .*"):
            pm.uninstall(self.get_test_plugin_path('plugin1'))


    def test_load_plugin_not_enabled(self):
        pm = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm.start()

        plugin_url = self.get_test_plugin_path('plugin1')
        plugin = pm.install(plugin_url)

        db_plugin = PluginData(name="plugin1", description="description",
                               app_url=plugin_url)
        plugin = pm.load_plugin(db_plugin, None)

        assert isinstance(plugin, InstalledPlugin)
        assert plugin.info() == {
            "name": "plugin1",
            "description": "description",
            "plugin_id": self.get_test_plugin_id('plugin1'),
            "enabled": False,
            "installed": True,
            "active": False,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }

    def test_load_plugin_enabled(self):
        pm = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm.start()

        plugin_url = self.get_test_plugin_path('plugin1')
        plugin = pm.install(plugin_url)

        db_plugin = PluginData(name="plugin1", description="description",
                               app_url=plugin_url, enabled=True)
        plugin = pm.load_plugin(db_plugin, "token")

        expected = {
            "name": "plugin1",
            "description": "description",
            "plugin_id": self.get_test_plugin_id('plugin1'),
            "enabled": True,
            "installed": True,
            "active": False,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }
        assert isinstance(plugin, RunnablePlugin)
        assert plugin.info() == expected

        # Try load_plugin another time.
        plugin = pm.load_plugin(db_plugin, "token")
        assert isinstance(plugin, RunnablePlugin)
        assert plugin.info() == expected

    def test_load_plugin_enabled_in_another_instance(self):
        pm1 = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm1.start()

        plugin_url = self.get_test_plugin_path('plugin1')
        plugin = pm1.install(plugin_url)

        db_plugin = PluginData(name="plugin1", description="description",
                               app_url=plugin_url, enabled=True)
        plugin = pm1.load_plugin(db_plugin, "token")

        pm2 = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm2.start()
        plugin = pm2.load_plugin(db_plugin, "token")

        expected = {
            "name": "plugin1",
            "description": "description",
            "plugin_id": self.get_test_plugin_id('plugin1'),
            "enabled": True,
            "installed": True,
            "active": False,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }
        assert isinstance(plugin, RunnablePlugin)
        assert plugin.info() == expected

    def test_load_plugin_disabled(self):
        pm = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm.start()

        plugin_url = self.get_test_plugin_path('plugin1')
        plugin = pm.install(plugin_url)

        db_plugin = PluginData(name="plugin1", description="description",
                               app_url=plugin_url, enabled=True)
        pm.load_plugin(db_plugin, "token")

        db_plugin.enabled = False
        plugin = pm.load_plugin(db_plugin, None)

        assert plugin.info() == {
            "name": "plugin1",
            "description": "description",
            "plugin_id": self.get_test_plugin_id('plugin1'),
            "enabled": False,
            "installed": True,
            "active": False,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }

    def test_activate(self):
        pm = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm.start()

        plugin_url = self.get_test_plugin_path('plugin1')
        plugin = pm.install(plugin_url)

        db_plugin = PluginData(name="plugin1", description="description",
                               app_url=plugin_url, enabled=True)
        pm.load_plugin(db_plugin, "token")
        plugin = pm.activate(plugin_url)

        expected = {
            "name": "plugin1",
            "description": "description",
            "plugin_id": self.get_test_plugin_id('plugin1'),
            "enabled": True,
            "installed": True,
            "active": True,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }
        assert plugin.info() == expected

        assert pm.activate(plugin_url).info() == expected

        plugin = pm.deactivate(plugin_url)
        expected = {
            "name": "plugin1",
            "description": "description",
            "plugin_id": self.get_test_plugin_id('plugin1'),
            "enabled": True,
            "installed": True,
            "active": False,
            "remote_url": self.get_test_plugin_path('plugin1'),
        }
        assert plugin.info() == expected

        pm.stop()

    def test_activate_disabled_plugin(self):
        pm = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm.start()

        plugin_url = self.get_test_plugin_path('plugin1')
        plugin = pm.install(plugin_url)

        db_plugin = PluginData(name="plugin1", description="description",
                               app_url=plugin_url)
        pm.load_plugin(db_plugin, None)

        with pytest.raises(PluginLoadError, match=".*not enabled.*"):
            pm.activate(plugin_url)

    def test_activate_remote_plugin(self):
        pm = PluginManager(self.base_dir, lister_fn=self.list_plugins)
        pm.start()

        plugin_url = self.get_test_plugin_path('plugin1')
        with pytest.raises(PluginLoadError, match=".*not installed.*"):
            pm.activate(plugin_url)
