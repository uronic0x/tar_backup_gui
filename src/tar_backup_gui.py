#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tar_backup_gui.py
批量小文件流式打包备份工具（PySide6 图形界面）

特性:
  - 自选多个文件夹，每个文件夹打包成一个 tar（流式，不落中间文件）
  - 可选 无 / gzip / zstd 压缩
  - 校验：整档 SHA256 + 条目清单(.mf)比对，打包时顺带算哈希，校验只顺序读一次 tar
  - 适合机械盘：顺序写入与校验，避开小文件随机寻道；校验不再逐个读源文件
  - 仅用 Python 标准库 + PySide6

用法:
    python tar_backup_gui.py             # 启动图形界面
    python tar_backup_gui.py --selftest  # 无界面自检(打包+校验核心逻辑)
"""

import os
import sys
import time
import json
import threading
import hashlib
import tarfile
import html

try:
    from PySide6.QtCore import Qt, QThread, Signal, Slot, QTimer
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QLineEdit, QPushButton, QListWidget, QListWidgetItem,
        QComboBox, QCheckBox, QProgressBar, QTextEdit, QFileDialog,
        QMessageBox, QAbstractItemView, QGroupBox
    )
    from PySide6.QtGui import QTextCursor
    QT_AVAILABLE = True
except ImportError:  # --selftest 不应因未安装 GUI 依赖而无法运行
    QT_AVAILABLE = False

    class _QtUnavailable:
        def __init__(self, *args, **kwargs):
            raise RuntimeError('图形界面需要安装 PySide6')

    class _DummySignal:
        def __get__(self, instance, owner):
            return self

        def connect(self, *args, **kwargs):
            raise RuntimeError('图形界面需要安装 PySide6')

        def emit(self, *args, **kwargs):
            raise RuntimeError('图形界面需要安装 PySide6')

    class _QtNamespace:
        ItemIsUserCheckable = 0
        Checked = 2
        Unchecked = 0
        Yes = 1
        No = 0

    Qt = _QtNamespace()
    QThread = QMainWindow = QWidget = QVBoxLayout = QHBoxLayout = _QtUnavailable
    QLabel = QLineEdit = QPushButton = QListWidget = QListWidgetItem = _QtUnavailable
    QComboBox = QCheckBox = QProgressBar = QTextEdit = QFileDialog = _QtUnavailable
    QMessageBox = QAbstractItemView = QGroupBox = QApplication = _QtUnavailable
    QTextCursor = _QtUnavailable
    QTimer = _QtUnavailable

    def Signal(*args, **kwargs):
        return _DummySignal()

    def Slot(*args, **kwargs):
        return lambda fn: fn


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
PROGRESS_STEPS = 1000       # 跨 Qt 信号的千分比，始终落在 32 位 int 范围内
PROGRESS_READ_CHUNK = 1 << 20


class Cancelled(Exception):
    """用户取消。"""


def win_long(path):
    r"""Windows 长路径支持：为绝对路径加 \\?\ 前缀以越过 260 字符限制。"""
    if sys.platform == 'win32':
        p = os.path.abspath(path)
        if p.startswith('\\\\?\\'):
            return p
        if p.startswith('\\\\'):
            return '\\\\?\\UNC\\' + p[2:]
        return '\\\\?\\' + p
    return path


def human_bytes(n):
    """把字节数格式化为易读单位。"""
    f = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(f) < 1024 or unit == 'TB':
            return f'{n} B' if unit == 'B' else f'{f:.1f} {unit}'
        f /= 1024
    return f'{f:.1f} TB'


def walk_tree(folder):
    """遍历 folder，yield (abspath, arcname, is_dir, size)。
    arcname 用正斜杠，适合 tar；含根目录条目 '.' 以保留空目录。"""
    folder = os.path.abspath(folder)
    yield (folder, '.', True, 0)
    for root, dirs, files in os.walk(folder):
        for d in sorted(dirs):
            full = os.path.join(root, d)
            yield (full, os.path.relpath(full, folder).replace(os.sep, '/'), True, 0)
        for f in sorted(files):
            full = os.path.join(root, f)
            try:
                size = os.path.getsize(win_long(full))
            except OSError:
                size = 0
            yield (full, os.path.relpath(full, folder).replace(os.sep, '/'), False, size)


def count_files(folder, skip=None):
    """统计普通文件数量（用于打包进度条分母）。skip 为 tar 自身路径则跳过。"""
    skip = os.path.abspath(skip) if skip else None
    n = 0
    for full, arc, is_dir, _ in walk_tree(folder):
        if is_dir or os.path.islink(win_long(full)):
            continue
        if skip and os.path.abspath(full) == skip:
            continue
        n += 1
    return n


def ext_for(compress):
    return {'无': '.tar', 'gzip': '.tar.gz', 'zstd': '.tar.zst'}[compress]


def mode_for(compress):
    return {'无': 'w', 'gzip': 'w:gz', 'zstd': 'w:zst'}[compress]


def available_compressions():
    """探测本机可用的压缩方式（zstd 在部分构建里不可用）。"""
    base = ['无', 'gzip']
    try:
        with tarfile.open(os.devnull, 'w:zst'):
            pass
        base.append('zstd')
    except Exception:
        pass
    return base


def unique_tar_name(out, base, compress):
    """输出目录内若重名则加时间戳，避免覆盖既有备份。"""
    ext = ext_for(compress)
    base = os.path.basename(os.path.normpath(base)) or 'backup'

    def occupied(name):
        tar_path = os.path.join(out, name)
        return (os.path.exists(win_long(tar_path)) or
                os.path.exists(win_long(manifest_path_for(tar_path))))

    name = base + ext
    if not occupied(name):
        return name
    ts = time.strftime('%Y%m%d_%H%M%S')
    for suffix in range(0, 100000):
        extra = f'_{ts}' if suffix == 0 else f'_{ts}_{suffix}'
        name = f'{base}{extra}{ext}'
        if not occupied(name):
            return name
    raise RuntimeError('无法生成不重复的归档文件名')


def safe_remove(path):
    try:
        os.remove(win_long(path))
    except OSError:
        pass


def manifest_path_for(tar_path):
    return tar_path + '.mf'


def write_manifest(mf_path, data):
    with open(win_long(mf_path), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def read_manifest(mf_path):
    with open(win_long(mf_path), 'r', encoding='utf-8') as f:
        return json.load(f)


class _HashingFile:
    """文件包装器：读/写时同步更新哈希，用于整档校验。
    close() 不关底层文件，由调用方管理（便于校验时排空尾部）。"""

    def __init__(self, f, hasher, read_callback=None):
        self._f = f
        self.hasher = hasher
        self._read_callback = read_callback
        self.closed = False

    def read(self, n=-1):
        b = self._f.read(n)
        self.hasher.update(b)
        if self._read_callback is not None:
            self._read_callback(self._f.tell())
        return b

    def write(self, b):
        self._f.write(b)
        self.hasher.update(b)
        return len(b)

    def flush(self):
        self._f.flush()

    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return False  # 强制流式行为

    def tell(self):
        return self._f.tell()

    def close(self):
        pass  # 不关底层


def progress_units(value, maximum, steps=PROGRESS_STEPS):
    """将任意大小的进度转换为安全的整数刻度，避免 Qt int 溢出。"""
    try:
        value = int(value)
        maximum = int(maximum)
    except (TypeError, ValueError):
        return 0
    if maximum <= 0:
        return steps if value > 0 else 0
    if value <= 0:
        return 0
    return min(steps, (value * steps) // maximum)


# --------------------------------------------------------------------------- #
# 核心逻辑（与界面无关，便于测试与复用）
# --------------------------------------------------------------------------- #
def _force_regular(ti, size):
    """tarfile 会把多硬链接文件存为 LNKTYPE(不写数据)，强制改为 REGTYPE 写完整数据。"""
    if ti.islnk():
        ti.type = tarfile.REGTYPE
        ti.linkname = ""
        ti.size = size
    return ti


def pack_folder(folder, tar_path, compress, skip_path, cancel, progress_cb):
    """流式打包：边读源文件边写 tar，不落中间文件；顺带算整档 SHA256。
    返回 (已加文件数, 字节数, 失败列表[(arcname, errmsg)], manifest字典)。"""
    mode = mode_for(compress)
    skip = os.path.abspath(skip_path) if skip_path else None
    errors, n_files, n_bytes = [], 0, 0
    src_files = {}  # arcname -> size，源端"应有"集合，写入清单供校验比对
    f = open(win_long(tar_path), 'wb')
    hw = _HashingFile(f, hashlib.sha256())
    try:
        tar = tarfile.open(fileobj=hw, mode=mode)
        try:
            for abspath, arcname, is_dir, size in walk_tree(folder):
                if cancel.is_set():
                    raise Cancelled()
                if skip and os.path.abspath(abspath) == skip:
                    continue  # 跳过正在写入的 tar 自身
                # 记录源端普通文件(不含符号链接)为"应有"集合
                if not is_dir:
                    is_link = os.path.islink(win_long(abspath))
                    if not is_link:
                        src_files[arcname] = size
                else:
                    is_link = False
                try:
                    tar.add(win_long(abspath), arcname, recursive=False,
                            filter=lambda ti: _force_regular(ti, size))
                except (OSError, tarfile.TarError) as e:
                    errors.append((arcname, f'{type(e).__name__}: {e}'))
                    continue
                if not is_dir and not is_link:
                    n_files += 1
                    n_bytes += size
                    progress_cb(n_files)
        finally:
            tar.close()  # 写入结束块/填充，全部流经 hw
        manifest = {
            'format': 1,
            'compress': compress,
            'added_count': n_files,
            'total_bytes': n_bytes,
            'files': src_files,            # 源端应有集合
            'sha256': hw.hasher.hexdigest(),  # 整档哈希
            'created': time.strftime('%Y-%m-%dT%H:%M:%S'),
        }
        return n_files, n_bytes, errors, manifest
    finally:
        f.close()


def verify_archive(tar_path, cancel, progress_cb, file_size):
    """整档校验：一次顺序读 tar，同步重算 SHA256 与枚举条目，再与 .mf 比对。
    不读取任何源文件。返回 (条目数, 问题列表[(kind, arcname, message)])。"""
    mf = manifest_path_for(tar_path)
    if not os.path.exists(win_long(mf)):
        return 0, [('no_manifest', '', '未找到校验清单 .mf，无法校验')]
    try:
        manifest = read_manifest(mf)
    except (OSError, ValueError, TypeError) as e:
        return 0, [('manifest', '', f'无法读取校验清单: {e}')]
    if not isinstance(manifest, dict):
        return 0, [('manifest', '', '校验清单格式错误：顶层必须是对象')]
    expected = manifest.get('files', {})
    if not isinstance(expected, dict):
        return 0, [('manifest', '', '校验清单格式错误：files 必须是对象')]
    expected_sha = manifest.get('sha256')

    last_reported = -1

    def report(position, force=False):
        """按已实际读到的归档字节数报告进度，并及时响应取消。"""
        nonlocal last_reported
        if cancel.is_set():
            raise Cancelled()
        position = max(0, int(position))
        if (force or last_reported < 0 or
                position - last_reported >= PROGRESS_READ_CHUNK or
                (file_size and position >= file_size)):
            last_reported = position
            progress_cb(position, file_size)

    f = open(win_long(tar_path), 'rb')
    hr = _HashingFile(f, hashlib.sha256(), read_callback=report)
    try:
        try:
            tar = tarfile.open(fileobj=hr, mode='r|*')  # 流式自动识别压缩
        except Cancelled:
            raise
        except Exception as e:
            return 0, [('open', '', f'无法打开归档: {e}')]

        actual = {}
        duplicate_entries = []
        n = 0
        try:
            for m in tar:
                if cancel.is_set():
                    raise Cancelled()
                if m.isfile():
                    if m.name in actual:
                        duplicate_entries.append(m.name)
                    actual[m.name] = m.size
                    n += 1
                report(f.tell())
        finally:
            tar.close()
        # 排空 tarfile 未读的尾部(结束块/填充)，保证哈希覆盖整档
        while True:
            if cancel.is_set():
                raise Cancelled()
            chunk = hr.read(PROGRESS_READ_CHUNK)
            if not chunk:
                break
        report(f.tell(), force=True)
        sha = hr.hasher.hexdigest()
    finally:
        f.close()

    issues = []
    if expected_sha and sha != expected_sha:
        issues.append(('sha256', '', '整档 SHA256 不一致（写入错误或位腐烂）'))
    for arc, size in expected.items():
        try:
            expected_size = int(size)
        except (TypeError, ValueError):
            issues.append(('manifest', arc, f'清单中的大小无效: {size!r}'))
            continue
        if arc not in actual:
            issues.append(('missing_archive', arc, '源文件未被备份进归档'))
        elif actual[arc] != expected_size:
            issues.append(('size', arc, f'大小不符 归档{actual[arc]}≠清单{expected_size}'))
    for arc in actual:
        if arc not in expected:
            issues.append(('extra', arc, '归档中存在清单未记录的文件'))
    issues.extend(('duplicate', arc, '归档中存在重复条目') for arc in duplicate_entries)
    return n, issues


# --------------------------------------------------------------------------- #
# 工作线程
# --------------------------------------------------------------------------- #
class BackupWorker(QThread):
    """后台工作线程，执行打包+校验任务。"""

    # 信号定义（done 避免遮蔽 QThread.finished）
    log_message = Signal(str, str)           # text, tag
    status_changed = Signal(str)             # status text
    # object 让 Python 大整数原样通过，避免 PySide 把字节数转换为 32 位 Qt int。
    progress_setup = Signal(object, object)  # value, maximum
    progress_update = Signal(object, object) # value, maximum
    done = Signal(dict)                       # 任务完成，携带结果 dict

    def __init__(self, folders, out_dir, compress, do_verify, parent=None):
        super().__init__(parent)
        self.folders = folders
        self.out_dir = out_dir
        self.compress = compress
        self.do_verify = do_verify
        self._cancel = threading.Event()

    def cancel(self):
        """请求取消。"""
        self._cancel.set()

    def is_cancelled(self):
        return self._cancel.is_set()

    def run(self):
        err_count = 0
        processed = 0
        try:
            for i, folder in enumerate(self.folders, 1):
                if self._cancel.is_set():
                    break
                name = os.path.basename(os.path.normpath(folder)) or folder
                tar_name = unique_tar_name(self.out_dir, name, self.compress)
                tar_path = os.path.join(self.out_dir, tar_name)
                mf_path = manifest_path_for(tar_path)

                # 打包（顺带算整档 SHA256，写入 .mf 清单）
                self.status_changed.emit(f'[{i}/{len(self.folders)}] 打包 {name} …')
                try:
                    total = count_files(folder, skip=tar_path)
                    progress_total = max(total, 1)
                    self.progress_setup.emit(0, progress_total)
                    n, b, errs, manifest = pack_folder(
                        folder, tar_path, self.compress, tar_path, self._cancel,
                        lambda nf: self.progress_update.emit(nf, progress_total))
                    self.progress_update.emit(progress_total, progress_total)
                except Cancelled:
                    safe_remove(tar_path)
                    safe_remove(mf_path)
                    self.log_message.emit(f'已取消，删除未完成归档: {tar_name}', 'err')
                    break
                except Exception as e:
                    safe_remove(tar_path)
                    safe_remove(mf_path)
                    self.log_message.emit(f'打包失败 {name}: {e}', 'err')
                    err_count += 1
                    processed += 1
                    continue

                try:
                    write_manifest(mf_path, manifest)
                except Exception as e:
                    safe_remove(tar_path)
                    safe_remove(mf_path)
                    self.log_message.emit(f'写入清单失败 {name}: {e}', 'err')
                    err_count += 1
                    processed += 1
                    continue

                tag = 'ok' if not errs else 'warn'
                self.log_message.emit(
                    f'打包完成 {name}: {n} 文件 / {human_bytes(b)} / {len(errs)} 个失败', tag)
                err_count += len(errs)

                # 校验：只顺序读 tar，不读源文件
                if self.do_verify and not self._cancel.is_set():
                    self.status_changed.emit(f'[{i}/{len(self.folders)}] 校验 {name} …')
                    try:
                        fsize = os.path.getsize(win_long(tar_path))
                        progress_total = max(fsize, 1)
                        self.progress_setup.emit(0, progress_total)
                        total_v, issues = verify_archive(
                            tar_path, self._cancel,
                            lambda br, fs: self.progress_update.emit(br, fs), fsize)
                    except Cancelled:
                        self.log_message.emit(f'校验中断 {name}', 'warn')
                        break
                    except Exception as e:
                        self.log_message.emit(f'校验出错 {name}: {e}', 'err')
                        err_count += 1
                        processed += 1
                        continue

                    self.progress_update.emit(progress_total, progress_total)
                    tag = 'ok' if not issues else 'warn'
                    self.log_message.emit(
                        f'校验完成 {name}: 比对 {total_v} 条目 / {len(issues)} 处不符', tag)
                    err_count += len(issues)
                    for kind, arc, msg in issues[:50]:
                        self.log_message.emit(f'    - [{kind}] {arc}: {msg}', 'warn')
                    if len(issues) > 50:
                        self.log_message.emit(f'    … 共 {len(issues)} 处，仅显示前 50', 'warn')
                processed += 1
        except Exception as e:
            # 防止未预料的异常让界面永远停留在“运行中”。
            err_count += 1
            self.log_message.emit(f'任务异常: {e}', 'err')
        finally:
            self.done.emit({
                'folders': len(self.folders),
                'processed': processed,
                'errors': err_count,
                'cancelled': self._cancel.is_set()
            })


# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #
class BackupWindow(QMainWindow):
    TITLE = '流式 tar 备份工具（每个文件夹一个归档 + 整档校验）'

    def __init__(self):
        super().__init__()
        self.worker = None
        self._build_ui()
        self.setWindowTitle(self.TITLE)
        self.resize(820, 640)
        self.setMinimumSize(680, 500)

    # ----- UI 构建 -----
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        # --- 文件夹列表 + 增删按钮 ---
        list_group = QGroupBox('待备份文件夹（勾选谁就打包谁，每个生成一个 tar）')
        list_outer = QVBoxLayout(list_group)
        list_outer.setSpacing(4)

        btn_row = QHBoxLayout()
        self.btn_add = QPushButton('+ 添加文件夹…')
        self.btn_add.clicked.connect(self.add_folder)
        btn_row.addWidget(self.btn_add)
        self.btn_remove = QPushButton('移除选中')
        self.btn_remove.clicked.connect(self.remove_selected)
        btn_row.addWidget(self.btn_remove)
        self.btn_select_all = QPushButton('全选')
        self.btn_select_all.clicked.connect(self.select_all)
        btn_row.addWidget(self.btn_select_all)
        self.btn_select_none = QPushButton('全不选')
        self.btn_select_none.clicked.connect(self.select_none)
        btn_row.addWidget(self.btn_select_none)
        btn_row.addStretch(1)
        list_outer.addLayout(btn_row)

        self.folder_list = QListWidget()
        self.folder_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.folder_list.setAlternatingRowColors(True)
        list_outer.addWidget(self.folder_list)
        layout.addWidget(list_group, 1)

        # --- 输出目录 ---
        dst_row = QHBoxLayout()
        dst_row.addWidget(QLabel('输出目录'))
        self.dst_edit = QLineEdit()
        self.dst_edit.setPlaceholderText('tar 文件存放处（建议为另一块盘）')
        dst_row.addWidget(self.dst_edit, 1)
        self.btn_browse_dst = QPushButton('浏览…')
        self.btn_browse_dst.clicked.connect(self.browse_dst)
        dst_row.addWidget(self.btn_browse_dst)
        layout.addLayout(dst_row)

        # --- 选项 ---
        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel('压缩:'))
        self.compress_cb = QComboBox()
        self.compress_cb.addItems(available_compressions())
        self.compress_cb.setMinimumWidth(80)
        opt_row.addWidget(self.compress_cb)
        opt_row.addSpacing(16)
        self.verify_chk = QCheckBox('打包后校验（整档SHA256 + 条目比对）')
        self.verify_chk.setChecked(True)
        opt_row.addWidget(self.verify_chk)
        opt_row.addStretch(1)
        layout.addLayout(opt_row)

        # --- 操作按钮 ---
        act_row = QHBoxLayout()
        self.btn_start = QPushButton('开始备份')
        self.btn_start.setStyleSheet('QPushButton { font-weight: bold; padding: 6px 20px; }')
        self.btn_start.clicked.connect(self.start)
        act_row.addWidget(self.btn_start)
        self.btn_cancel = QPushButton('取消')
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.do_cancel)
        act_row.addWidget(self.btn_cancel)
        act_row.addStretch(1)
        layout.addLayout(act_row)

        # --- 进度 ---
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.status_label = QLabel('就绪。')
        layout.addWidget(self.status_label)

        # --- 日志 ---
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QTextEdit.WidgetWidth)
        self.log.document().setMaximumBlockCount(10000)  # 限制内存占用
        layout.addWidget(self.log, 1)

    # ----- 文件夹增删 -----
    @Slot()
    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, '选择要备份的文件夹')
        if d:
            # 去重：已存在则跳过
            existing = {os.path.normcase(os.path.abspath(self.folder_list.item(i).text()))
                        for i in range(self.folder_list.count())}
            if os.path.normcase(os.path.abspath(d)) in existing:
                QMessageBox.information(self, '提示', '该文件夹已在列表中。')
                return
            item = QListWidgetItem(d)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.folder_list.addItem(item)

    @Slot()
    def remove_selected(self):
        # 从末尾往前删，避免索引错位
        rows = sorted(
            (self.folder_list.row(it) for it in self.folder_list.selectedItems()),
            reverse=True)
        for r in rows:
            self.folder_list.takeItem(r)

    @Slot()
    def select_all(self):
        for i in range(self.folder_list.count()):
            self.folder_list.item(i).setCheckState(Qt.Checked)

    @Slot()
    def select_none(self):
        for i in range(self.folder_list.count()):
            self.folder_list.item(i).setCheckState(Qt.Unchecked)

    def selected_folders(self):
        return [
            self.folder_list.item(i).text()
            for i in range(self.folder_list.count())
            if self.folder_list.item(i).checkState() == Qt.Checked
        ]

    # ----- 输出目录 -----
    @Slot()
    def browse_dst(self):
        d = QFileDialog.getExistingDirectory(self, '选择输出目录')
        if d:
            self.dst_edit.setText(d)

    # ----- 启动/取消 -----
    @Slot()
    def start(self):
        folders = self.selected_folders()
        out = self.dst_edit.text().strip()
        if not folders:
            QMessageBox.warning(self, '提示', '请先添加并勾选要备份的文件夹。')
            return
        if not out or not os.path.isdir(out):
            QMessageBox.warning(self, '提示', '请选择有效的输出目录。')
            return
        missing = [f for f in folders if not os.path.isdir(win_long(f))]
        if missing:
            QMessageBox.warning(self, '提示', '以下文件夹已不存在或无法访问：\n' + '\n'.join(missing))
            return
        # 输出在被备份文件夹内部 → 提醒(已自动跳过 tar 自身)
        out_abs = os.path.realpath(os.path.abspath(out))
        for f in folders:
            fa = os.path.realpath(os.path.abspath(f))
            try:
                inside = os.path.commonpath((out_abs, fa)) == fa
            except ValueError:  # Windows 不同盘符
                inside = False
            if inside:
                ret = QMessageBox.question(
                    self, '提醒',
                    '输出目录位于被备份的文件夹内部，已自动跳过归档自身。是否继续？',
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if ret != QMessageBox.Yes:
                    return
                break

        self._set_running(True)
        self._log(f'开始备份：{len(folders)} 个文件夹 → {out}', 'ok')

        self.worker = BackupWorker(
            folders, out, self.compress_cb.currentText(), self.verify_chk.isChecked())
        self.worker.log_message.connect(self._log)
        self.worker.status_changed.connect(self._set_status)
        self.worker.progress_setup.connect(self._progress_setup)
        self.worker.progress_update.connect(self._progress_update)
        self.worker.done.connect(self._on_finished)
        self.worker.start()

    @Slot()
    def do_cancel(self):
        if self.worker:
            self.worker.cancel()
            self._set_status('正在取消…')

    def closeEvent(self, event):
        """关闭窗口：若仍在运行则先取消，等当前文件夹结束后再退出，避免半成品 tar。"""
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self._log('已请求取消，当前文件夹结束后可关闭窗口。', 'warn')
            event.ignore()
            QTimer.singleShot(500, self.close)
            return
        event.accept()

    # ----- 信号处理 -----
    @Slot(str, str)
    def _log(self, text, tag=None):
        color_map = {'ok': '#1a7d1a', 'warn': '#b8860b', 'err': '#c62828'}
        color = color_map.get(tag)
        safe_text = html.escape(str(text))
        if color:
            self.log.append(f'<span style="color:{color}">{safe_text}</span>')
        else:
            self.log.append(safe_text)
        # 滚动到底部
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log.setTextCursor(cursor)

    @Slot(str)
    def _set_status(self, text):
        self.status_label.setText(text)

    @Slot(object, object)
    def _progress_setup(self, value, maximum):
        self._set_progress(value, maximum)

    @Slot(object, object)
    def _progress_update(self, value, maximum):
        self._set_progress(value, maximum)

    def _set_progress(self, value, maximum):
        """只向 QProgressBar 写入安全的小整数范围。"""
        if maximum > PROGRESS_STEPS:
            value = progress_units(value, maximum)
            maximum = PROGRESS_STEPS
        else:
            maximum = max(int(maximum), 1)
            value = min(max(int(value), 0), maximum)
        self.progress.setRange(0, maximum)
        self.progress.setValue(value)

    @Slot(dict)
    def _on_finished(self, d):
        self._set_running(False)
        msg = '已取消。' if d['cancelled'] else '全部完成。'
        tag = 'warn' if d['errors'] else 'ok'
        processed = d.get('processed', d['folders'])
        self._log(f"{msg} 已处理 {processed}/{d['folders']} 个文件夹，错误/不符 {d['errors']} 处。", tag)
        self._set_status(msg)
        QMessageBox.information(self, '完成', f"{msg}\n错误/不符: {d['errors']}")

    # ----- 辅助 -----
    def _set_running(self, running):
        self.btn_start.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        self.folder_list.setEnabled(not running)
        self.btn_add.setEnabled(not running)
        self.btn_remove.setEnabled(not running)
        self.btn_select_all.setEnabled(not running)
        self.btn_select_none.setEnabled(not running)
        self.btn_browse_dst.setEnabled(not running)
        self.compress_cb.setEnabled(not running)
        self.verify_chk.setEnabled(not running)


# --------------------------------------------------------------------------- #
# 自检（无界面，验证核心逻辑）
# --------------------------------------------------------------------------- #
def selftest():
    import tempfile, shutil
    tmp = tempfile.mkdtemp(prefix='tar_selftest_')
    print(f'自检目录: {tmp}')
    try:
        # 进度通过 Qt 信号时不得携带大于 32 位有符号 int 的字节数。
        assert progress_units(2_600_000_000, 2_600_000_000) == PROGRESS_STEPS
        assert progress_units(1_300_000_000, 2_600_000_000) == PROGRESS_STEPS // 2
        assert 0 <= progress_units(99, 2_600_000_000) <= PROGRESS_STEPS

        src = os.path.join(tmp, 'src')
        out = os.path.join(tmp, 'out')
        os.makedirs(out)
        os.makedirs(os.path.join(src, 'a', 'sub'))
        os.makedirs(os.path.join(src, 'b'))
        files = {
            os.path.join(src, 'a', 'f1.txt'): b'hello',
            os.path.join(src, 'a', 'sub', 'f2.bin'): bytes(range(256)) * 4,
            os.path.join(src, 'b', 'g1.txt'): b'world' * 100,
            os.path.join(src, 'b', 'empty.txt'): b'',
        }
        for p, c in files.items():
            with open(p, 'wb') as fh:
                fh.write(c)
        cancel = threading.Event()

        # 1) 当前可用的压缩方式：打包+写清单+校验，均应无问题
        for comp in available_compressions():
            folder = os.path.join(src, 'a') if comp == '无' else os.path.join(src, 'b')
            tp = os.path.join(out, f'{os.path.basename(folder)}{ext_for(comp)}')
            total = count_files(folder, skip=tp)
            n, b, errs, mf = pack_folder(folder, tp, comp, tp, cancel, lambda nf: None)
            write_manifest(manifest_path_for(tp), mf)
            fsize = os.path.getsize(win_long(tp))
            n_v, issues = verify_archive(tp, cancel, lambda br, fs: None, fsize)
            print(f'  {comp}: 打包 {n} 文件 / 失败 {len(errs)} / 校验条目 {n_v} / 不符 {len(issues)}')
            assert not errs, f'{comp} 打包有失败: {errs}'
            assert not issues, f'{comp} 校验有不符: {issues}'

        # 2) 破坏未压缩 tar 的数据字节 → 整档 SHA256 不一致(条目仍可枚举)
        tp = os.path.join(out, 'a.tar')
        with open(win_long(tp), 'r+b') as fb:
            fb.seek(600)  # 落在 PAX 数据填充区，不影响枚举但改变哈希
            orig = fb.read(1)
            fb.seek(600)
            fb.write(bytes([orig[0] ^ 0xFF]))
        fsize = os.path.getsize(win_long(tp))
        _, issues = verify_archive(tp, cancel, lambda br, fs: None, fsize)
        assert any(k == 'sha256' for k, _, _ in issues), '破坏后应检出 sha256 不一致'
        print('  翻转 tar 数据字节后，校验检出整档 SHA256 不一致。')

        # 3) 篡改清单：增加一个归档里没有的"应有"文件 → missing_archive
        tp2 = os.path.join(out, 'b.tar')
        n, b, errs, mf = pack_folder(os.path.join(src, 'b'), tp2, '无', tp2, cancel, lambda nf: None)
        write_manifest(manifest_path_for(tp2), mf)
        mf['files']['__fake__.txt'] = 123
        write_manifest(manifest_path_for(tp2), mf)
        fsize = os.path.getsize(win_long(tp2))
        _, issues = verify_archive(tp2, cancel, lambda br, fs: None, fsize)
        assert any(k == 'missing_archive' and a == '__fake__.txt' for k, a, _ in issues), \
            '应检出 missing_archive'
        print('  清单含虚假条目时，校验检出 missing_archive。')

        # 4) 校验读流期间取消应立即抛出 Cancelled，而不是被误报成 open 错误。
        verify_cancel = threading.Event()
        seen_progress = []

        def cancel_on_progress(br, fs):
            seen_progress.append(br)
            verify_cancel.set()

        try:
            verify_archive(tp2, verify_cancel, cancel_on_progress, fsize)
        except Cancelled:
            assert seen_progress, '取消测试应至少收到一次进度回调'
            print('  校验读流期间取消可及时生效。')
        else:
            raise AssertionError('校验取消应抛出 Cancelled')

        # 5) 清单损坏时返回可见的问题，而不是让后台线程悄悄退出。
        with open(manifest_path_for(tp2), 'w', encoding='utf-8') as fh:
            fh.write('{')
        _, issues = verify_archive(tp2, cancel, lambda br, fs: None, fsize)
        assert any(k == 'manifest' for k, _, _ in issues), '损坏清单应报告 manifest 问题'
        print('  清单损坏时，校验检出 manifest 问题。')
        print('自检通过。')
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    if '--selftest' in sys.argv:
        return selftest()
    if not QT_AVAILABLE:
        print('启动图形界面需要安装 PySide6；可使用 --selftest 运行无界面自检。',
              file=sys.stderr)
        return 2
    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # 跨平台统一风格
    window = BackupWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
