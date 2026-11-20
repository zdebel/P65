"""Storage manager window: the emulator's storage image on the left, the real
device (USB serial) on the right. Copy files either way, import / export files
of the PC (BASIC text is tokenized on import and listed on export), view,
rename, delete, format."""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

import p65dev
import storage as S
import storageio as IO


def mono():
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)


def fmt_bytes(n):
    return '%d KB' % (n // 1024) if n >= 10 * 1024 else '%d B' % n


class ImportDialog(QDialog):
    """Name, type and addresses for a file coming from the PC."""

    def __init__(self, parent, spec, host_name):
        super().__init__(parent)
        self.setWindowTitle('Import ' + host_name)
        self.spec = spec
        f = QFormLayout(self)
        # (attribute names must not shadow QDialog methods such as exec())
        self.name_edit = QLineEdit(p65dev.format_name(spec['name11']))
        self.name_edit.setFont(mono())
        self.type_box = QComboBox()
        for t, label in IO.TYPE_LABEL.items():
            self.type_box.addItem('%s  (%s)' % (label, {0: 'machine code / data', 1: 'tokenized BASIC program', 2: 'text'}[t]), t)
        self.type_box.setCurrentIndex(spec['type'])
        self.load_edit = QLineEdit('%04X' % spec['load'])
        self.exec_edit = QLineEdit('%04X' % (spec['exec'] or (spec['load'] if spec['type'] == 0 else 0)))   # BIN: shown explicitly, default = load
        for w in (self.load_edit, self.exec_edit):
            w.setFont(mono())
            w.setMaximumWidth(80)
        f.addRow('File name (8.3)', self.name_edit)
        f.addRow('Type', self.type_box)
        f.addRow('Load address $', self.load_edit)
        f.addRow('Exec address $ (BRUN starts here)', self.exec_edit)
        f.addRow(QLabel('%d bytes' % len(spec['data'])))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def result_spec(self):
        t = self.type_box.currentData()
        spec = dict(self.spec)
        spec['type'] = t
        spec['name11'] = p65dev.parse_name(self.name_edit.text().strip(), IO.TYPE_LABEL[t])
        spec['load'] = int(self.load_edit.text().strip().lstrip('$') or '0', 16) & 0xFFFF
        spec['exec'] = int(self.exec_edit.text().strip().lstrip('$') or '0', 16) & 0xFFFF
        if t == 0 and spec['exec'] == 0:
            spec['exec'] = spec['load']                  # 0 would only mean 'same as load': store it explicitly
        return spec


class FilePanel(QGroupBox):
    """One list of files (a backend) with its buttons."""
    status = Signal(str)

    def __init__(self, title, owner):
        super().__init__(title)
        self.owner = owner
        self.backend = None
        self.entries = []
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Name', 'Type', 'Size', 'Load', 'Exec'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setFont(mono())
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for c in (1, 2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemDoubleClicked.connect(lambda _: self.view())
        lay.addWidget(self.table, 1)
        self.info = QLabel('not connected')
        lay.addWidget(self.info)
        row = QHBoxLayout()
        self.buttons = {}
        for key, text, slot in (('import', 'Import...', self.import_files), ('export', 'Export...', self.export_files),
                                ('view', 'View', self.view), ('rename', 'Rename', self.rename),
                                ('delete', 'Delete', self.delete), ('format', 'Format', self.format)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
            self.buttons[key] = b
        lay.addLayout(row)

    # -- state ---------------------------------------------------------------------------
    def set_backend(self, backend):
        self.backend = backend
        self.refresh()

    def refresh(self):
        self.table.setRowCount(0)
        self.entries = []
        for b in self.buttons.values():
            b.setEnabled(self.backend is not None)
        if self.backend is None:
            self.info.setText('not connected')
            return
        try:
            self.entries = self.backend.list()
            inf = self.backend.info()
        except Exception as e:                          # noqa: BLE001 - show any device problem
            self.info.setText('error: %s' % e)
            return
        self.table.setRowCount(len(self.entries))
        for r, e in enumerate(self.entries):
            cells = [e['name'], IO.TYPE_LABEL.get(e['type'], '???'), str(e['size']), '$%04X' % e['load'], IO.exec_text(e)]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if c in (2, 3, 4):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, it)
        self.info.setText('%d files, %s free of %s' % (inf['files'], fmt_bytes(inf['free']), fmt_bytes(inf['total'])))

    def selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.entries[r] for r in rows if r < len(self.entries)]

    def _one(self):
        sel = self.selected()
        if len(sel) != 1:
            self.status.emit('select one file')
            return None
        return sel[0]

    def _error(self, what, exc):
        QMessageBox.warning(self, what, str(exc))
        self.status.emit('%s: %s' % (what, exc))

    # -- actions -----------------------------------------------------------------------------
    def import_files(self, paths=None):
        if self.backend is None:
            return
        if not paths:
            paths, _ = QFileDialog.getOpenFileNames(self, 'Import files', self.owner.last_dir,
                                                    'BASIC, binary, text (*.bas *.bin *.txt *.prg *.rom);;All files (*)')
        for p in paths:
            self.owner.last_dir = os.path.dirname(p)
            try:
                spec = IO.import_host_file(p)
                dlg = ImportDialog(self, spec, os.path.basename(p))
                if dlg.exec() != QDialog.DialogCode.Accepted:
                    continue
                spec = dlg.result_spec()
                if any(e['name11'] == spec['name11'] for e in self.entries):
                    r = QMessageBox.question(self, 'Replace?', '%s exists. Replace it?' % p65dev.format_name(spec['name11']))
                    if r != QMessageBox.StandardButton.Yes:
                        continue
                self.owner.busy('Writing %s...' % p65dev.format_name(spec['name11']))
                self.backend.write(spec['name11'], spec['data'], spec['type'], spec['load'], spec['exec'], self.owner.progress)
                self.status.emit('imported %s (%d bytes)' % (p65dev.format_name(spec['name11']), len(spec['data'])))
            except Exception as e:                      # noqa: BLE001
                self._error('Import ' + os.path.basename(p), e)
        self.owner.idle()
        self.owner.refresh_all()

    def export_files(self):
        for e in self.selected():
            try:
                as_text = True
                if e['type'] == S.TYPE_BAS:
                    box = QMessageBox(self)
                    box.setWindowTitle('Export ' + e['name'])
                    box.setText('Save the BASIC program as a text listing, or as the raw tokenized file?')
                    t_btn = box.addButton('Text listing', QMessageBox.ButtonRole.AcceptRole)
                    r_btn = box.addButton('Raw tokenized', QMessageBox.ButtonRole.AcceptRole)
                    box.addButton(QMessageBox.StandardButton.Cancel)
                    box.exec()
                    if box.clickedButton() is t_btn:
                        as_text = True
                    elif box.clickedButton() is r_btn:
                        as_text = False
                    else:
                        continue
                data = self.backend.read(e, self.owner.progress)
                name, out = IO.export_file(e, data, as_text)
                if e['type'] == S.TYPE_BAS and as_text:
                    name = os.path.splitext(name)[0] + '.bas'
                path, _ = QFileDialog.getSaveFileName(self, 'Save ' + e['name'], os.path.join(self.owner.last_dir, name))
                if path:
                    self.owner.last_dir = os.path.dirname(path)
                    with open(path, 'wb') as f:
                        f.write(out)
                    self.status.emit('exported %s' % path)
            except Exception as ex:                     # noqa: BLE001
                self._error('Export', ex)
        self.owner.idle()

    def view(self):
        e = self._one()
        if not e:
            return
        try:
            data = self.backend.read(e, self.owner.progress)
        except Exception as ex:                         # noqa: BLE001
            self._error('View', ex)
            return
        self.owner.idle()
        d = QDialog(self)
        d.setWindowTitle('%s  (%d bytes)' % (e['name'], e['size']))
        v = QVBoxLayout(d)
        t = QPlainTextEdit(IO.view_text(e, data))
        t.setReadOnly(True)
        t.setFont(mono())
        t.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        v.addWidget(t)
        d.resize(720, 480)
        d.exec()

    def rename(self):
        e = self._one()
        if not e:
            return
        text, ok = QInputDialog.getText(self, 'Rename', 'New name for %s:' % e['name'], text=e['name'])
        if not ok:
            return
        try:
            new11 = p65dev.parse_name(text.strip(), IO.TYPE_LABEL.get(e['type'], 'BIN'))
            self.backend.rename(e['name11'], new11)
        except Exception as ex:                         # noqa: BLE001
            self._error('Rename', ex)
        self.owner.refresh_all()

    def delete(self):
        sel = self.selected()
        if not sel:
            return
        names = ', '.join(e['name'] for e in sel[:5]) + (' ...' if len(sel) > 5 else '')
        if QMessageBox.question(self, 'Delete', 'Delete %d file(s)?\n%s' % (len(sel), names)) != QMessageBox.StandardButton.Yes:
            return
        for e in sel:
            try:
                self.backend.delete(e['name11'])
            except Exception as ex:                     # noqa: BLE001
                self._error('Delete', ex)
                break
        self.owner.refresh_all()

    def format(self):
        if QMessageBox.question(self, 'Format', 'Delete ALL files in %s?' % self.title(),
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.format()
        except Exception as ex:                         # noqa: BLE001
            self._error('Format', ex)
        self.owner.refresh_all()

    # -- drag and drop of PC files ---------------------------------------------------------------
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        self.import_files([u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()])


class StorageDialog(QDialog):
    def __init__(self, parent, store, last_dir=''):
        super().__init__(parent)
        self.setWindowTitle('Storage manager')
        self.setModal(False)
        self.last_dir = last_dir or os.path.expanduser('~')
        self.device = None
        self.serial = None

        top = QVBoxLayout(self)
        conn = QHBoxLayout()
        conn.addWidget(QLabel('Device port:'))
        self.ports = QComboBox()
        self.ports.setMinimumWidth(260)
        conn.addWidget(self.ports)
        b = QPushButton('Rescan')
        b.clicked.connect(self.scan_ports)
        conn.addWidget(b)
        self.connect_btn = QPushButton('Connect')
        self.connect_btn.clicked.connect(self.toggle_connection)
        conn.addWidget(self.connect_btn)
        conn.addStretch(1)
        top.addLayout(conn)

        panels = QHBoxLayout()
        self.local = FilePanel('Emulator storage', self)
        self.remote = FilePanel('Device (USB)', self)
        panels.addWidget(self.local, 1)
        mid = QVBoxLayout()
        mid.addStretch(1)
        self.to_dev = QPushButton('  ->  ')
        self.to_dev.setToolTip('Copy the selected emulator files to the device')
        self.to_dev.clicked.connect(lambda: self.copy(self.local, self.remote))
        self.to_emu = QPushButton('  <-  ')
        self.to_emu.setToolTip('Copy the selected device files to the emulator storage')
        self.to_emu.clicked.connect(lambda: self.copy(self.remote, self.local))
        mid.addWidget(self.to_dev)
        mid.addWidget(self.to_emu)
        mid.addStretch(1)
        panels.addLayout(mid)
        panels.addWidget(self.remote, 1)
        top.addLayout(panels, 1)

        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        self.bar.setMaximumHeight(14)
        self.bar.hide()
        top.addWidget(self.bar)
        self.msg = QLabel('')
        top.addWidget(self.msg)
        for p in (self.local, self.remote):
            p.status.connect(self.msg.setText)

        self.set_store(store)
        self.remote.set_backend(None)
        self.scan_ports()
        self.resize(980, 520)

    # -- backends -------------------------------------------------------------------------------
    def set_store(self, store):
        self.local.set_backend(IO.LocalBackend(store) if store is not None else None)
        if store is None:
            self.local.info.setText('p65store.dll missing - run emulator/build_storelib.py')

    def scan_ports(self):
        cur = self.ports.currentData()
        self.ports.clear()
        for dev, desc in p65dev.list_serial_ports():
            self.ports.addItem('%s  %s' % (dev, desc), dev)
        if self.ports.count() == 0:
            self.ports.addItem('(no serial ports found)', None)
        i = self.ports.findData(cur)
        if i >= 0:
            self.ports.setCurrentIndex(i)

    def toggle_connection(self):
        if self.device is not None:
            self.disconnect_device()
            return
        port = self.ports.currentData()
        if not port:
            self.msg.setText('no serial port selected')
            return
        try:
            ser = p65dev.open_serial(port)
        except Exception as e:                          # noqa: BLE001
            QMessageBox.warning(self, 'Connect', 'Cannot open %s:\n%s' % (port, e))
            return
        self.connect_transport(ser, port)

    def connect_transport(self, transport, label='device'):
        """Use an already open transport (a pyserial port, or a loopback in the tests)."""
        dev = p65dev.P65Device(transport, timeout=2.0)
        try:
            h = dev.hello()
        except Exception as e:                          # noqa: BLE001
            if hasattr(transport, 'close'):
                transport.close()
            QMessageBox.warning(self, 'Connect', 'No answer from the device on %s:\n%s\n\n'
                                'It needs the firmware with storage support.' % (label, e))
            return False
        self.device, self.serial = dev, transport
        self.remote.set_backend(IO.DeviceBackend(dev))
        self.connect_btn.setText('Disconnect')
        self.msg.setText('connected to %s: %s, protocol v%d' % (label, h['text'], h['version']))
        return True

    def disconnect_device(self):
        if self.serial is not None and hasattr(self.serial, 'close'):
            try:
                self.serial.close()
            except Exception:                           # noqa: BLE001
                pass
        self.device = self.serial = None
        self.remote.set_backend(None)
        self.connect_btn.setText('Connect')
        self.msg.setText('disconnected')

    # -- helpers used by the panels ----------------------------------------------------------------
    def refresh_all(self):
        self.local.refresh()
        self.remote.refresh()

    def busy(self, text):
        self.msg.setText(text)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.show()
        QApplication.processEvents()

    def idle(self):
        self.bar.hide()
        QApplication.processEvents()

    def progress(self, done, total):
        self.bar.setValue(int(100 * done / total) if total else 100)
        QApplication.processEvents()

    def copy(self, src, dst):
        if src.backend is None or dst.backend is None:
            self.msg.setText('both sides must be available (connect the device first)')
            return
        sel = src.selected()
        if not sel:
            self.msg.setText('select files to copy')
            return
        existing = {e['name11'] for e in dst.entries}
        done = 0
        for e in sel:
            if e['name11'] in existing:
                r = QMessageBox.question(self, 'Replace?', '%s already exists on the other side. Replace it?' % e['name'])
                if r != QMessageBox.StandardButton.Yes:
                    continue
            try:
                self.busy('Copying %s...' % e['name'])
                IO.copy_file(src.backend, dst.backend, e, self.progress)
                done += 1
            except Exception as ex:                     # noqa: BLE001
                QMessageBox.warning(self, 'Copy', '%s: %s' % (e['name'], ex))
                break
        self.idle()
        self.msg.setText('copied %d file(s)' % done)
        self.refresh_all()

    def closeEvent(self, ev):
        self.disconnect_device()
        super().closeEvent(ev)
