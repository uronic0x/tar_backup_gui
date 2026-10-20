# tar-backup-gui

[English](#english) | [简体中文](#简体中文)

---

<a name="简体中文"></a>

## 简体中文

批量小文件流式打包备份工具，带 PySide6 图形界面。

### 特性

- 自选多个文件夹，每个文件夹打包成一个 tar（流式写入，不落中间文件）
- 可选压缩：无 / gzip / zstd（自动探测本机可用性）
- 完整性校验：整档 SHA256 + 条目清单（`.mf`）比对，打包时顺带计算哈希，校验只需顺序读一次归档，不读源文件
- 对机械硬盘友好：全程顺序读写，避开海量小文件的随机寻道
- 取消安全：取消或失败时自动删除半成品归档
- Windows 长路径（>260 字符）支持
- 仅依赖 Python 标准库 + PySide6

### 安装

```bash
pip install PySide6
```

### 使用

```bash
python src/tar_backup_gui.py             # 启动图形界面
python src/tar_backup_gui.py --selftest  # 无界面自检（打包+校验核心逻辑）
```

1. 添加并勾选要备份的文件夹
2. 选择输出目录（建议是另一块物理盘）
3. 选择压缩方式，点击「开始备份」
4. 打包完成后会自动进行整档校验（可关闭）

### 校验清单格式（`.mf`）

每个归档旁边会生成一个同名 `.mf` JSON 清单，记录：压缩方式、文件数、字节数、
每个源文件的相对路径与大小、整档 SHA256、创建时间。日后可用它复查归档完整性。

### 许可证

[MIT](LICENSE)

---

<a name="english"></a>

## English

A streaming tar backup tool for large sets of small files, with a PySide6 GUI.

### Features

- Pick multiple folders; each one is packed into its own tar (streaming write, no intermediate files)
- Optional compression: none / gzip / zstd (availability auto-detected)
- Integrity verification: whole-archive SHA256 + entry manifest (`.mf`) comparison. The hash is computed while packing, so verification only needs one sequential read of the archive — source files are never re-read
- HDD friendly: sequential reads/writes throughout, avoiding random seeks over huge numbers of small files
- Cancel-safe: incomplete archives are removed on cancel or failure
- Windows long-path (>260 chars) support
- Depends only on the Python standard library + PySide6

### Install

```bash
pip install PySide6
```

### Usage

```bash
python src/tar_backup_gui.py             # launch the GUI
python src/tar_backup_gui.py --selftest  # headless self-test (pack + verify core logic)
```

1. Add and check the folders to back up
2. Choose an output directory (preferably another physical drive)
3. Choose a compression method and click "开始备份" (Start Backup)
4. After packing, the archive is verified automatically (can be disabled)

### Manifest format (`.mf`)

Each archive is accompanied by a sibling `.mf` JSON manifest recording: compression,
file count, byte count, relative path and size of every source file, whole-archive
SHA256, and creation time. Use it later to re-check archive integrity.

### License

[MIT](LICENSE)
