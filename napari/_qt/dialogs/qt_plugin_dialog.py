import os
import sys
from pathlib import Path
from typing import Sequence

from napari_plugin_engine.dist import standard_metadata
from napari_plugin_engine.exceptions import PluginError
from qtpy.QtCore import QEvent, QProcess, QProcessEnvironment, QSize, Qt, Slot
from qtpy.QtGui import QFont, QMovie
from qtpy.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from typing_extensions import Literal

import napari.resources

from ...plugins import plugin_manager
from ...plugins.pypi import (
    ProjectInfo,
    iter_napari_plugin_info,
    normalized_name,
)
from ...utils._appdirs import user_plugin_dir, user_site_packages
from ...utils.misc import parse_version, running_as_bundled_app
from ...utils.translations import trans
from ..qthreading import create_worker
from ..widgets.qt_eliding_label import ElidingLabel
from ..widgets.qt_plugin_sorter import QtPluginSorter
from .qt_plugin_report import QtPluginErrReporter

InstallerTypes = Literal['pip', 'conda', 'mamba']


# TODO: add error icon and handle pip install errors
# TODO: add queue to handle clicks when already processing
class Installer:
    def __init__(
        self,
        output_widget: QTextEdit = None,
        installer: InstallerTypes = "pip",
    ):
        from ...plugins import plugin_manager

        # To be used when conda is fully supported
        self._conda_env_path = None

        if installer != "pip" and (Path(sys.prefix) / "conda-meta").is_dir():
            self._conda_env_path = sys.prefix

        # create install process
        self._output_widget = None
        self.process = QProcess()
        if installer != "pip":
            self.process.setProgram(installer)
        else:
            self.process.setProgram(sys.executable)

        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._on_stdout_ready)
        # setup process path
        env = QProcessEnvironment()
        combined_paths = os.pathsep.join(
            [user_site_packages(), env.systemEnvironment().value("PYTHONPATH")]
        )
        env.insert("PYTHONPATH", combined_paths)
        # use path of parent process
        env.insert(
            "PATH", QProcessEnvironment.systemEnvironment().value("PATH")
        )
        self.process.setProcessEnvironment(env)
        self.process.finished.connect(lambda: plugin_manager.discover())
        self.process.finished.connect(lambda: plugin_manager.prune())
        self.set_output_widget(output_widget)

    def set_output_widget(self, output_widget: QTextEdit):
        if output_widget:
            self._output_widget = output_widget
            self.process.setParent(output_widget)

    def _on_stdout_ready(self):
        if self._output_widget:
            text = self.process.readAllStandardOutput().data().decode()
            self._output_widget.append(text)

    def install(
        self,
        pkg_list: Sequence[str],
        installer: InstallerTypes = "pip",
        channels: Sequence[str] = ("conda-forge",),
    ):
        if installer != "pip":
            cmd = [
                'install',
                '-y',
                '--prefix',
                self._conda_env_path,
            ]
            for channel in channels:
                cmd.extend(["-c", channel])
        else:
            cmd = ['-m', 'pip', 'install', '--upgrade']

        if (
            running_as_bundled_app()
            and sys.platform.startswith('linux')
            and not self._use_conda
        ):
            cmd += [
                '--no-warn-script-location',
                '--prefix',
                user_plugin_dir(),
            ]

        self.process.setArguments(cmd + list(pkg_list))
        if self._output_widget:
            self._output_widget.clear()
        self.process.start()

    def uninstall(
        self,
        pkg_list: Sequence[str],
        installer: InstallerTypes = "pip",
        channels: Sequence[str] = ("conda-forge",),
    ):
        if installer != "pip":
            args = [
                'remove',
                '-y',
                '--prefix',
                self._conda_env_path,
            ]

            for channel in channels:
                args.extend(["-c", channel])
        else:
            args = ['-m', 'pip', 'uninstall', '-y']

        self.process.setArguments(args + list(pkg_list))
        if self._output_widget:
            self._output_widget.clear()

        self.process.start()

        for pkg in pkg_list:
            plugin_manager.unregister(pkg)

    @staticmethod
    def _is_installed_with_conda():
        """
        Check if conda was used to install qt and napari.
        """
        from qtpy import QT_VERSION

        from ..._version import version_tuple

        parts = [str(part) for part in version_tuple[:3]]
        napari_version_string = f"napari-{'.'.join(parts)}-"
        qt_version_string = f"qt-{QT_VERSION}-"
        conda_meta_path = Path(sys.prefix) / "conda-meta"
        if conda_meta_path.is_dir():
            for file in conda_meta_path.iterdir():
                fname = file.parts[-1]
                if fname.startswith(napari_version_string) and fname.endswith(
                    ".json"
                ):
                    return True
                elif fname.startswith(qt_version_string) and fname.endswith(
                    ".json"
                ):
                    return True
            else:
                return False


class PluginListItem(QFrame):
    def __init__(
        self,
        package_name: str,
        version: str = '',
        url: str = '',
        summary: str = '',
        author: str = '',
        license: str = "UNKNOWN",
        *,
        plugin_name: str = None,
        parent: QWidget = None,
        enabled: bool = True,
    ):
        super().__init__(parent)
        self.setup_ui(enabled)
        if plugin_name:
            self.plugin_name.setText(plugin_name)
            self.package_name.setText(f"{package_name} {version}")
            self.summary.setText(summary)
            self.package_author.setText(author)
            self.action_button.setText(trans._("uninstall"))
            self.action_button.setObjectName("remove_button")
            self.enabled_checkbox.setChecked(enabled)
            if PluginError.get(plugin_name=plugin_name):

                def _show_error():
                    rep = QtPluginErrReporter(
                        parent=self._get_dialog(), initial_plugin=plugin_name
                    )
                    rep.setWindowFlags(Qt.Sheet)
                    close = QPushButton(trans._("close"), rep)
                    rep.layout.addWidget(close)
                    rep.plugin_combo.hide()
                    close.clicked.connect(rep.close)
                    rep.open()

                self.error_indicator.clicked.connect(_show_error)
                self.error_indicator.show()
                self.summary.setIndent(18)
            else:
                self.summary.setIndent(38)
        else:
            self.plugin_name.setText(package_name)
            self.package_name.setText(version)
            self.summary.setText(summary)
            self.package_author.setText(author)
            self.action_button.setText(trans._("install"))
            self.enabled_checkbox.hide()

    def _get_dialog(self) -> QDialog:
        p = self.parent()
        while not isinstance(p, QDialog) and p.parent():
            p = p.parent()
        return p

    def setup_ui(self, enabled=True):
        self.v_lay = QVBoxLayout(self)
        self.v_lay.setContentsMargins(-1, 6, -1, 6)
        self.v_lay.setSpacing(0)
        self.row1 = QHBoxLayout()
        self.row1.setSpacing(6)
        self.enabled_checkbox = QCheckBox(self)
        self.enabled_checkbox.setChecked(enabled)
        self.enabled_checkbox.stateChanged.connect(self._on_enabled_checkbox)
        self.enabled_checkbox.setToolTip(trans._("enable/disable"))
        sizePolicy = QSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(
            self.enabled_checkbox.sizePolicy().hasHeightForWidth()
        )
        self.enabled_checkbox.setSizePolicy(sizePolicy)
        self.enabled_checkbox.setMinimumSize(QSize(20, 0))
        self.enabled_checkbox.setText("")
        self.row1.addWidget(self.enabled_checkbox)
        self.plugin_name = QLabel(self)
        sizePolicy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(
            self.plugin_name.sizePolicy().hasHeightForWidth()
        )
        self.plugin_name.setSizePolicy(sizePolicy)
        font15 = QFont()
        font15.setPointSize(15)
        self.plugin_name.setFont(font15)
        self.row1.addWidget(self.plugin_name)
        self.package_name = QLabel(self)
        self.package_name.setAlignment(
            Qt.AlignRight | Qt.AlignTrailing | Qt.AlignVCenter
        )
        self.row1.addWidget(self.package_name)
        self.action_button = QPushButton(self)
        sizePolicy = QSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(
            self.action_button.sizePolicy().hasHeightForWidth()
        )
        self.action_button.setSizePolicy(sizePolicy)
        self.row1.addWidget(self.action_button)
        self.v_lay.addLayout(self.row1)
        self.row2 = QHBoxLayout()
        self.error_indicator = QPushButton()
        self.error_indicator.setObjectName("warning_icon")
        self.error_indicator.setCursor(Qt.PointingHandCursor)
        self.error_indicator.hide()
        self.row2.addWidget(self.error_indicator)
        self.row2.setContentsMargins(-1, 4, 0, -1)
        self.summary = ElidingLabel(parent=self)
        sizePolicy = QSizePolicy(
            QSizePolicy.MinimumExpanding, QSizePolicy.Preferred
        )
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(
            self.summary.sizePolicy().hasHeightForWidth()
        )
        self.summary.setSizePolicy(sizePolicy)
        self.summary.setObjectName("small_text")
        self.row2.addWidget(self.summary)
        self.package_author = QLabel(self)
        sizePolicy = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(
            self.package_author.sizePolicy().hasHeightForWidth()
        )
        self.package_author.setSizePolicy(sizePolicy)
        self.package_author.setObjectName("small_text")
        self.row2.addWidget(self.package_author)
        self.v_lay.addLayout(self.row2)

    def _on_enabled_checkbox(self, state: int):
        """Called with `state` when checkbox is clicked."""
        plugin_manager.set_blocked(self.plugin_name.text(), not state)


class QPluginList(QListWidget):
    def __init__(self, parent: QWidget, installer: Installer):
        super().__init__(parent)
        self.installer = installer
        self.setSortingEnabled(True)

    @Slot(ProjectInfo)
    def addItem(
        self, project_info: ProjectInfo, plugin_name=None, enabled=True
    ):
        # don't add duplicates
        if (
            self.findItems(project_info.name, Qt.MatchFixedString)
            and not plugin_name
        ):
            return

        # including summary here for sake of filtering below.
        searchable_text = project_info.name + " " + project_info.summary
        item = QListWidgetItem(searchable_text, parent=self)
        item.version = project_info.version
        super().addItem(item)

        widg = PluginListItem(
            *project_info,
            parent=self,
            plugin_name=plugin_name,
            enabled=enabled,
        )
        method = getattr(
            self.installer, 'uninstall' if plugin_name else 'install'
        )
        widg.action_button.clicked.connect(lambda: method([project_info.name]))

        item.setSizeHint(widg.sizeHint())
        self.setItemWidget(item, widg)

    @Slot(ProjectInfo)
    def tag_outdated(self, project_info: ProjectInfo):
        for item in self.findItems(project_info.name, Qt.MatchFixedString):
            current = item.version
            latest = project_info.version
            if parse_version(current) >= parse_version(latest):
                continue
            if hasattr(item, 'outdated'):
                # already tagged it
                continue
            item.outdated = True
            widg = self.itemWidget(item)
            update_btn = QPushButton(
                trans._("update (v{latest})", latest=latest), widg
            )
            update_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            update_btn.clicked.connect(
                lambda: self.installer.install([item.text()])
            )
            widg.row1.insertWidget(3, update_btn)

    def filter(self, text: str):
        """Filter items to those containing `text`."""
        shown = self.findItems(text, Qt.MatchContains)
        for i in range(self.count()):
            item = self.item(i)
            item.setHidden(item not in shown)


class QtPluginDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.installer = Installer()
        self.setup_ui()
        self.installer.set_output_widget(self.stdout_text)
        self.installer.process.started.connect(self._on_installer_start)
        self.installer.process.finished.connect(self._on_installer_done)
        self.refresh()

    def _on_installer_start(self):
        self.show_status_btn.setChecked(True)
        self.working_indicator.show()
        self.process_error_indicator.hide()

    def _on_installer_done(self, exit_code, exit_status):
        self.working_indicator.hide()
        if exit_code:
            self.process_error_indicator.show()
        else:
            self.show_status_btn.setChecked(False)
        self.refresh()
        self.plugin_sorter.refresh()

    def refresh(self):
        self.installed_list.clear()
        self.available_list.clear()

        # fetch installed
        from ...plugins import plugin_manager

        plugin_manager.discover()  # since they might not be loaded yet

        already_installed = set()

        for plugin_name, mod_name, distname in plugin_manager.iter_available():
            # not showing these in the plugin dialog
            if plugin_name in ('napari_plugin_engine',):
                continue
            if distname:
                already_installed.add(distname)
                meta = standard_metadata(distname)
            else:
                meta = {}
            self.installed_list.addItem(
                ProjectInfo(
                    normalized_name(distname or ''),
                    meta.get('version', ''),
                    meta.get('url', ''),
                    meta.get('summary', ''),
                    meta.get('author', ''),
                    meta.get('license', ''),
                ),
                plugin_name=plugin_name,
                enabled=plugin_name in plugin_manager.plugins,
            )
        # self.v_splitter.setSizes([70 * self.installed_list.count(), 10, 10])

        # fetch available plugins
        self.worker = create_worker(iter_napari_plugin_info)

        def _handle_yield(project_info):
            if project_info.name in already_installed:
                self.installed_list.tag_outdated(project_info)
            else:
                self.available_list.addItem(project_info)

        self.worker.yielded.connect(_handle_yield)
        self.worker.finished.connect(self.working_indicator.hide)
        self.worker.finished.connect(self._update_count_in_label)
        self.worker.start()

    def setup_ui(self):
        self.resize(1080, 640)
        vlay_1 = QVBoxLayout(self)
        self.h_splitter = QSplitter(self)
        vlay_1.addWidget(self.h_splitter)
        self.h_splitter.setOrientation(Qt.Horizontal)
        self.v_splitter = QSplitter(self.h_splitter)
        self.v_splitter.setOrientation(Qt.Vertical)
        self.v_splitter.setMinimumWidth(500)
        self.plugin_sorter = QtPluginSorter(parent=self.h_splitter)
        self.plugin_sorter.layout().setContentsMargins(2, 0, 0, 0)
        self.plugin_sorter.hide()

        installed = QWidget(self.v_splitter)
        lay = QVBoxLayout(installed)
        lay.setContentsMargins(0, 2, 0, 2)
        self.installed_label = QLabel(trans._("Installed Plugins"))
        self.installed_filter = QLineEdit()
        self.installed_filter.setPlaceholderText("search...")
        self.installed_filter.setMaximumWidth(350)
        self.installed_filter.setClearButtonEnabled(True)
        mid_layout = QHBoxLayout()
        mid_layout.addWidget(self.installed_label)
        mid_layout.addWidget(self.installed_filter)
        mid_layout.addStretch()
        lay.addLayout(mid_layout)

        self.installed_list = QPluginList(installed, self.installer)
        self.installed_filter.textChanged.connect(self.installed_list.filter)
        lay.addWidget(self.installed_list)

        uninstalled = QWidget(self.v_splitter)
        lay = QVBoxLayout(uninstalled)
        lay.setContentsMargins(0, 2, 0, 2)
        self.avail_label = QLabel(trans._("Available Plugins"))
        self.avail_filter = QLineEdit()
        self.avail_filter.setPlaceholderText("search...")
        self.avail_filter.setMaximumWidth(350)
        self.avail_filter.setClearButtonEnabled(True)
        mid_layout = QHBoxLayout()
        mid_layout.addWidget(self.avail_label)
        mid_layout.addWidget(self.avail_filter)
        mid_layout.addStretch()
        lay.addLayout(mid_layout)
        self.available_list = QPluginList(uninstalled, self.installer)
        self.avail_filter.textChanged.connect(self.available_list.filter)
        lay.addWidget(self.available_list)

        self.stdout_text = QTextEdit(self.v_splitter)
        self.stdout_text.setReadOnly(True)
        self.stdout_text.setObjectName("pip_install_status")
        self.stdout_text.hide()

        buttonBox = QHBoxLayout()
        self.working_indicator = QLabel(trans._("loading ..."), self)
        sp = self.working_indicator.sizePolicy()
        sp.setRetainSizeWhenHidden(True)
        self.working_indicator.setSizePolicy(sp)
        self.process_error_indicator = QLabel(self)
        self.process_error_indicator.setObjectName("error_label")
        self.process_error_indicator.hide()
        load_gif = str(Path(napari.resources.__file__).parent / "loading.gif")
        mov = QMovie(load_gif)
        mov.setScaledSize(QSize(18, 18))
        self.working_indicator.setMovie(mov)
        mov.start()

        self.direct_entry_edit = QLineEdit(self)
        self.direct_entry_edit.installEventFilter(self)
        self.direct_entry_edit.setPlaceholderText(
            trans._('install by name/url, or drop file...')
        )
        self.direct_entry_btn = QPushButton(trans._("Install"), self)
        self.direct_entry_btn.clicked.connect(self._install_packages)

        self.show_status_btn = QPushButton(trans._("Show Status"), self)
        self.show_status_btn.setFixedWidth(100)
        self.show_sorter_btn = QPushButton(trans._("<< Show Sorter"), self)
        self.close_btn = QPushButton(trans._("Close"), self)
        self.close_btn.clicked.connect(self.accept)
        buttonBox.addWidget(self.show_status_btn)
        buttonBox.addWidget(self.working_indicator)
        buttonBox.addWidget(self.direct_entry_edit)
        buttonBox.addWidget(self.direct_entry_btn)
        buttonBox.addWidget(self.process_error_indicator)
        buttonBox.addSpacing(60)
        buttonBox.addWidget(self.show_sorter_btn)
        buttonBox.addWidget(self.close_btn)
        buttonBox.setContentsMargins(0, 0, 4, 0)
        vlay_1.addLayout(buttonBox)

        self.show_status_btn.setCheckable(True)
        self.show_status_btn.setChecked(False)
        self.show_status_btn.toggled.connect(self._toggle_status)

        self.show_sorter_btn.setCheckable(True)
        self.show_sorter_btn.setChecked(False)
        self.show_sorter_btn.toggled.connect(self._toggle_sorter)

        self.v_splitter.setStretchFactor(1, 2)
        self.h_splitter.setStretchFactor(0, 2)

        self.avail_filter.setFocus()

    def _update_count_in_label(self):
        count = self.available_list.count()
        self.avail_label.setText(
            trans._("Available Plugins ({count})", count=count)
        )

    def eventFilter(self, watched, event):
        if event.type() == QEvent.DragEnter:
            # we need to accept this event explicitly to be able
            # to receive QDropEvents!
            event.accept()
        if event.type() == QEvent.Drop:
            md = event.mimeData()
            if md.hasUrls():
                files = [url.toLocalFile() for url in md.urls()]
                self.direct_entry_edit.setText(files[0])
                return True
        return super().eventFilter(watched, event)

    def _toggle_sorter(self, show):
        if show:
            self.show_sorter_btn.setText(trans._(">> Hide Sorter"))
            self.plugin_sorter.show()
        else:
            self.show_sorter_btn.setText(trans._("<< Show Sorter"))
            self.plugin_sorter.hide()

    def _toggle_status(self, show):
        if show:
            self.show_status_btn.setText(trans._("Hide Status"))
            self.stdout_text.show()
        else:
            self.show_status_btn.setText(trans._("Show Status"))
            self.stdout_text.hide()

    def _install_packages(self, packages: Sequence[str] = ()):
        if not packages:
            _packages = self.direct_entry_edit.text()
            if os.path.exists(_packages):
                packages = [_packages]
            else:
                packages = _packages.split()
            self.direct_entry_edit.clear()
        if packages:
            self.installer.install(packages)


if __name__ == "__main__":
    from qtpy.QtWidgets import QApplication

    app = QApplication([])
    w = QtPluginDialog()
    w.show()
    app.exec_()
