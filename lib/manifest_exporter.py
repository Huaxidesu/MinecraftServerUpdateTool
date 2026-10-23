#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minecraft 整合包 server-manifest.json 导出工具

用途：
- 扫描指定整合包目录，生成与 updater.py 兼容的 SHA-1 文件清单。
- 首次运行创建持久化配置；后续直接复用扫描目录、导出目录与 fileApi。
- 通过“文件大小 + 纳秒级修改时间”缓存未变化文件的哈希，大幅减少重复导出耗时。
- 只对新增/发生变化/缓存失效的文件重新读取并计算哈希。
- 导出后统计新增、修改、删除、未变化、缓存复用、重新哈希等信息。

哈希规则与 updater.py 保持一致：
- 指定二进制扩展名：直接对原始字节做 SHA-1。
- 其他文件：若内容是合法 UTF-8，则先把 CRLF 换行为 LF，再做 SHA-1；
  若不是合法 UTF-8，则按原始字节做 SHA-1。

清单条目结构：
- path  ：文件在整合包目录里的相对路径（磁盘上的真实大小写）。
- hash  ：SHA-1。
- urlPath：可选。仅当该文件在 git 仓库里的路径大小写与 path 不同时才写入，
           更新器拼下载地址时优先用它，避免因 Windows 大小写不敏感导致的 404。

发布物：
- <exportDir>/server-manifest.json        本次导出的正式清单（带真实版本号）。
- <sourceDir>/update/updater.py            更新器本体，由 client/update/updater.py 同步而来，
                                          玩家端靠它自更新。
- <exportDir>/client/server-manifest.json 客户端分发包的基线清单，版本号固定 1.0，
                                          只做统一起点，导出器不会用新清单覆盖它。
"""

import codecs
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, InvalidOperation
from pathlib import Path


CONFIG_FILE_NAME = 'manifest-exporter-config.json'
DEFAULT_MANIFEST_FILE = 'server-manifest.json'
DEFAULT_CACHE_FILE = '.manifest-hash-cache.json'
DEFAULT_HASH_THREADS = 8
MAX_HASH_THREADS = 32
CACHE_FORMAT_VERSION = 1
HASH_RULE_VERSION = 1
HASH_CHUNK_SIZE = 1024 * 1024

# 更新器源文件（相对导出目录）。导出时会把它同步到扫描目录下的 update/updater.py，
# 让它进入清单——玩家端的更新器就能通过普通更新流程把自己更新掉。
DEFAULT_UPDATER_SOURCE = 'client/update/updater.py'
# 更新器在整合包目录里的相对路径，同时也是玩家端的自更新落点。
UPDATER_TARGET_REL = 'update/updater.py'

# 客户端分发包里的基线清单（相对导出目录）。
# 它随包发给玩家、会覆盖玩家本地的清单，因此版本号**固定**为 1.0：
# 现在玩家手上的版本五花八门，统一压回 1.0 才能让每个人走同一条对齐路径。
# 注意：导出时绝不把新清单写进这里，这个文件只做「统一起点」，不追版本。
DEFAULT_CLIENT_MANIFEST = 'client/server-manifest.json'
DEFAULT_CLIENT_BASELINE_VERSION = '1.0'

BINARY_EXTENSIONS = {
    '.jar', '.zip', '.png', '.jpg', '.jpeg', '.gif', '.bmp',
    '.ico', '.ttf', '.otf', '.woff', '.woff2', '.mp3', '.ogg',
    '.wav', '.mp4', '.avi', '.mov', '.class', '.dat', '.bin'
}


class Colors:
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    GRAY = '\033[90m'
    BOLD = '\033[1m'
    RESET = '\033[0m'


if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        Colors.CYAN = Colors.GREEN = Colors.YELLOW = Colors.RED = ''
        Colors.GRAY = Colors.BOLD = Colors.RESET = ''


def print_color(text='', color=Colors.RESET):
    print(f'{color}{text}{Colors.RESET}')


def normalize_user_path(value):
    value = str(value or '').strip().strip('"').strip("'")
    value = os.path.expandvars(os.path.expanduser(value))
    return os.path.abspath(value) if value else ''


def normalize_file_api(value):
    value = str(value or '').strip().strip('"').strip("'")
    if not value:
        return ''
    return value.rstrip('/') + '/'


def is_valid_file_api(value):
    return bool(re.match(r'^https?://[^\s]+$', value or '', re.IGNORECASE))


def is_valid_pack_dir(path):
    """防止选错层级：要求扫描根目录直接包含 config 与 mods。"""
    if not path or not os.path.isdir(path):
        return False, '目录不存在'
    missing = []
    for name in ('config', 'mods'):
        if not os.path.isdir(os.path.join(path, name)):
            missing.append(name)
    if missing:
        return False, f"缺少关键目录: {', '.join(missing)}"
    return True, ''


def atomic_write_json(path, data):
    """先写临时文件再替换，避免中途退出留下半个 JSON。"""
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp_path = path + f'.tmp-{os.getpid()}'
    try:
        with open(temp_path, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


def load_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        print_color(f'警告: 无法读取 {path}: {e}', Colors.YELLOW)
        return default


def find_existing_manifest(script_dir, source_dir='', export_dir=''):
    """优先读取自定义导出目录中的旧清单，其次尝试工具目录和扫描目录。"""
    candidates = []
    if export_dir:
        candidates.append(os.path.join(export_dir, DEFAULT_MANIFEST_FILE))
    candidates.append(os.path.join(script_dir, DEFAULT_MANIFEST_FILE))
    if source_dir:
        candidates.append(os.path.join(source_dir, DEFAULT_MANIFEST_FILE))

    seen = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        if os.path.isfile(candidate):
            data = load_json(candidate)
            if isinstance(data, dict):
                return os.path.abspath(candidate), data

    default_dir = export_dir or script_dir
    return os.path.join(default_dir, DEFAULT_MANIFEST_FILE), None


def sanitize_threads(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_HASH_THREADS
    return max(1, min(MAX_HASH_THREADS, value))


def _derive_export_dir_from_legacy_config(script_dir, config):
    """兼容旧版只有 manifestFile、没有 exportDir 的配置。"""
    export_dir = normalize_user_path(config.get('exportDir', ''))
    if export_dir:
        return export_dir

    manifest_value = str(config.get('manifestFile', DEFAULT_MANIFEST_FILE) or DEFAULT_MANIFEST_FILE)
    if os.path.isabs(manifest_value):
        return os.path.dirname(os.path.abspath(manifest_value))

    legacy_path = os.path.abspath(os.path.join(script_dir, manifest_value))
    return os.path.dirname(legacy_path)


def setup_or_load_config(script_dir):
    config_path = os.path.join(script_dir, CONFIG_FILE_NAME)
    config = load_json(config_path)

    if isinstance(config, dict):
        source_dir = normalize_user_path(config.get('sourceDir', ''))
        file_api = normalize_file_api(config.get('fileApi', ''))
        export_dir = _derive_export_dir_from_legacy_config(script_dir, config)
        valid, _ = is_valid_pack_dir(source_dir)
        if valid and is_valid_file_api(file_api) and export_dir:
            try:
                os.makedirs(export_dir, exist_ok=True)
            except OSError as e:
                print_color(f'现有导出目录不可用: {e}', Colors.YELLOW)
            else:
                config['sourceDir'] = source_dir
                config['fileApi'] = file_api
                config['exportDir'] = export_dir
                config['hashThreads'] = sanitize_threads(config.get('hashThreads', DEFAULT_HASH_THREADS))
                # manifestFile 在新版配置中只保存文件名；导出目录由 exportDir 决定。
                manifest_value = str(config.get('manifestFile', DEFAULT_MANIFEST_FILE) or DEFAULT_MANIFEST_FILE)
                config['manifestFile'] = os.path.basename(manifest_value) or DEFAULT_MANIFEST_FILE
                config.setdefault('hashCacheFile', DEFAULT_CACHE_FILE)
                return config, config_path, False

        print_color('现有导出配置无效，将重新进行首次配置。', Colors.YELLOW)

    print_color('=' * 62, Colors.CYAN)
    print_color('  首次运行：创建 Manifest 导出配置', Colors.CYAN)
    print_color('=' * 62, Colors.CYAN)
    print()
    print('请选择“真正要打包到 overrides 里的整合包根目录”。')
    print('正确目录中应当能直接看到 config 和 mods 文件夹。')
    print_color(r'示例: D:\Game\Minecraft\HMCL\.minecraft\versions\GT New Horizons 2.8.3', Colors.GRAY)
    print_color(r'示例: D:\Game\Minecraft\MMCproject\instances\GT_New_Horizons\.minecraft', Colors.GRAY)
    print()

    while True:
        source_dir = normalize_user_path(input('请粘贴整合包目录: '))
        valid, reason = is_valid_pack_dir(source_dir)
        if valid:
            print_color(f'目录验证通过: {source_dir}', Colors.GREEN)
            break
        print_color(f'目录验证失败: {reason}', Colors.RED)
        print_color('请确认粘贴的是直接包含 config 和 mods 的那一级目录。', Colors.YELLOW)
        print()

    print()
    print('请选择 server-manifest.json 的导出目录。')
    print('可以直接填写你的 Git 仓库/服务器文件目录；目录不存在时会自动创建。')
    print_color(f'直接回车默认导出到脚本所在目录: {script_dir}', Colors.GRAY)
    while True:
        raw_export = input('导出目录: ').strip()
        export_dir = normalize_user_path(raw_export) if raw_export else script_dir
        try:
            os.makedirs(export_dir, exist_ok=True)
        except OSError as e:
            print_color(f'无法创建或使用该导出目录: {e}', Colors.RED)
            continue
        if not os.path.isdir(export_dir):
            print_color('导出路径不是文件夹，请重新输入。', Colors.RED)
            continue
        print_color(f'导出目录: {export_dir}', Colors.GREEN)
        break

    _, existing_manifest = find_existing_manifest(script_dir, source_dir, export_dir)
    existing_api = normalize_file_api((existing_manifest or {}).get('fileApi', ''))

    print()
    print('请输入更新域名 / fileApi（可填写完整子路径）。')
    print_color('示例: https://git.example.com/user/repo/raw/branch/main/290/', Colors.GRAY)
    if existing_api:
        print_color(f'检测到旧清单地址，直接回车可沿用: {existing_api}', Colors.GRAY)

    while True:
        raw = input('更新域名: ').strip()
        file_api = normalize_file_api(raw or existing_api)
        if is_valid_file_api(file_api):
            break
        print_color('地址无效，请输入以 http:// 或 https:// 开头的地址。', Colors.RED)

    config = {
        'sourceDir': source_dir,
        'exportDir': export_dir,
        'fileApi': file_api,
        'manifestFile': DEFAULT_MANIFEST_FILE,
        'hashCacheFile': DEFAULT_CACHE_FILE,
        'hashThreads': DEFAULT_HASH_THREADS
    }
    atomic_write_json(config_path, config)
    print_color(f'已创建配置文件: {config_path}', Colors.GREEN)
    print_color(f'哈希线程数默认: {DEFAULT_HASH_THREADS}（可在配置文件中修改，最高 {MAX_HASH_THREADS}）', Colors.GRAY)
    print()
    return config, config_path, True


def manifest_path_from_config(script_dir, config):
    export_dir = _derive_export_dir_from_legacy_config(script_dir, config)
    value = str(config.get('manifestFile', DEFAULT_MANIFEST_FILE) or DEFAULT_MANIFEST_FILE)
    file_name = os.path.basename(value) or DEFAULT_MANIFEST_FILE
    return os.path.join(export_dir, file_name)


def cache_path_from_config(script_dir, config):
    value = str(config.get('hashCacheFile', DEFAULT_CACHE_FILE) or DEFAULT_CACHE_FILE)
    return value if os.path.isabs(value) else os.path.join(script_dir, value)


def get_manifest_metadata(manifest, source_dir):
    """读取已有清单元数据；没有旧清单时才做最少量补充询问。"""
    if isinstance(manifest, dict):
        name = str(manifest.get('name', '') or '').strip() or os.path.basename(source_dir)
        author = str(manifest.get('author', '') or '')
        version = str(manifest.get('version', '1.0') or '1.0')
        description = str(manifest.get('description', '') or '')
        addons = manifest.get('addons', [])
        if not isinstance(addons, list):
            addons = []
        return name, author, version, description, addons

    print_color('未找到旧 server-manifest.json，无法自动读取整合包名字与版本。', Colors.YELLOW)
    default_name = os.path.basename(os.path.normpath(source_dir)) or 'Minecraft Pack'
    raw_name = input(f'整合包名称 [{default_name}]: ').strip()
    name = raw_name or default_name
    author = input('作者（可留空）: ').strip()
    raw_version = input('初始版本号 [1.0]: ').strip()
    version = raw_version or '1.0'
    return name, author, version, '', []


def default_next_version(version):
    """
    常见 1.0/1.9 形式按十进制 +0.1；
    2.9.0 之类多段版本则最后一段 +1。
    """
    version = str(version or '').strip()
    if re.fullmatch(r'\d+(?:\.\d+)?', version):
        try:
            result = Decimal(version) + Decimal('0.1')
            # 至少保留一位小数，避免 2.0 变成 2
            text = format(result, 'f')
            if '.' not in text:
                text += '.0'
            return text
        except InvalidOperation:
            pass

    match = re.fullmatch(r'(.*?)(\d+)([^\d]*)', version)
    if match:
        prefix, number, suffix = match.groups()
        return f'{prefix}{int(number) + 1}{suffix}'
    return version + '.1' if version else '1.0'


def prompt_new_version(current_version):
    default_version = default_next_version(current_version)
    print_color(f'当前版本: {current_version}', Colors.GREEN)
    raw = input(f'请输入新版本号 [直接回车默认 {default_version}]: ').strip()
    return raw or default_version


def norm_abs(path):
    return os.path.normcase(os.path.abspath(path))


def path_is_inside(parent, child):
    """跨盘符也安全的包含关系判断（Windows 不同盘符时 commonpath 会抛异常）。"""
    try:
        return os.path.commonpath([norm_abs(parent), norm_abs(child)]) == norm_abs(parent)
    except (ValueError, OSError):
        return False


def build_excluded_paths(script_dir, source_dir, config_path, manifest_path, cache_path):
    """若工具恰好放进扫描目录，避免把自己/配置/清单/缓存哈希进去。"""
    candidates = {
        norm_abs(__file__),
        norm_abs(config_path),
        norm_abs(manifest_path),
        norm_abs(cache_path),
    }
    return {p for p in candidates if path_is_inside(source_dir, p)}


def scan_source_files(source_dir, excluded_abs_paths):
    """使用 os.scandir 迭代扫描，避免 Path.rglob 的额外对象开销。"""
    source_dir = os.path.abspath(source_dir)
    source_norm = norm_abs(source_dir)
    result = []
    stack = [source_dir]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue

                        abs_key = norm_abs(entry.path)
                        if abs_key in excluded_abs_paths:
                            continue

                        stat = entry.stat(follow_symlinks=False)
                        rel = os.path.relpath(entry.path, source_dir).replace('\\', '/')
                        # 防御性检查：绝不允许生成越出扫描根目录的路径
                        if rel == '..' or rel.startswith('../') or os.path.isabs(rel):
                            continue
                        result.append((rel, entry.path, stat.st_size, stat.st_mtime_ns))
                    except (FileNotFoundError, PermissionError, OSError) as e:
                        raise RuntimeError(f'无法读取文件信息: {entry.path}: {e}') from e
        except (FileNotFoundError, PermissionError, OSError) as e:
            raise RuntimeError(f'无法扫描目录: {current}: {e}') from e

    result.sort(key=lambda item: item[0].casefold())
    return result


def is_binary_file(path):
    return os.path.splitext(path)[1].lower() in BINARY_EXTENSIONS


def calculate_compatible_sha1(file_path):
    """
    流式实现 updater.py 的哈希规则，避免大文件一次性读入内存。
    返回 (sha1, 实际读取字节数)。
    """
    raw_hasher = hashlib.sha1()
    total_read = 0

    if is_binary_file(file_path):
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                total_read += len(chunk)
                raw_hasher.update(chunk)
        return raw_hasher.hexdigest(), total_read

    normalized_hasher = hashlib.sha1()
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    valid_utf8 = True
    pending_cr = False

    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)
            if not chunk:
                break
            total_read += len(chunk)
            raw_hasher.update(chunk)

            if valid_utf8:
                try:
                    decoder.decode(chunk, final=False)
                except UnicodeDecodeError:
                    valid_utf8 = False

            if valid_utf8:
                if pending_cr:
                    chunk = b'\r' + chunk
                    pending_cr = False
                if chunk.endswith(b'\r'):
                    pending_cr = True
                    chunk = chunk[:-1]
                if chunk:
                    normalized_hasher.update(chunk.replace(b'\r\n', b'\n'))

    if valid_utf8:
        try:
            decoder.decode(b'', final=True)
        except UnicodeDecodeError:
            valid_utf8 = False

    if valid_utf8:
        if pending_cr:
            normalized_hasher.update(b'\r')
        return normalized_hasher.hexdigest(), total_read
    return raw_hasher.hexdigest(), total_read


def load_hash_cache(cache_path, source_dir):
    cache = load_json(cache_path, default={})
    if not isinstance(cache, dict):
        return {}
    if cache.get('formatVersion') != CACHE_FORMAT_VERSION:
        return {}
    if cache.get('hashRuleVersion') != HASH_RULE_VERSION:
        return {}
    if norm_abs(cache.get('sourceDir', '')) != norm_abs(source_dir):
        return {}
    files = cache.get('files', {})
    return files if isinstance(files, dict) else {}


def hash_files_with_cache(scanned_files, cache_files, threads):
    hashes = {}
    new_cache = {}
    cache_hits = 0
    bytes_read = 0
    misses = []

    for rel, abs_path, size, mtime_ns in scanned_files:
        cached = cache_files.get(rel)
        if (
            isinstance(cached, dict)
            and cached.get('size') == size
            and cached.get('mtimeNs') == mtime_ns
            and isinstance(cached.get('hash'), str)
            and re.fullmatch(r'[0-9a-fA-F]{40}', cached.get('hash', ''))
        ):
            digest = cached['hash'].lower()
            hashes[rel] = digest
            new_cache[rel] = {'size': size, 'mtimeNs': mtime_ns, 'hash': digest}
            cache_hits += 1
        else:
            misses.append((rel, abs_path, size, mtime_ns))

    errors = []
    completed = 0
    total_misses = len(misses)
    progress_step = max(100, min(1000, total_misses // 20 if total_misses else 100))

    def worker(item):
        rel, abs_path, size, mtime_ns = item
        digest, read_bytes = calculate_compatible_sha1(abs_path)

        # 如果扫描后文件仍在被游戏/同步软件修改，避免把“中间状态”写进清单和缓存。
        stat_after = os.stat(abs_path, follow_symlinks=False)
        final_size = stat_after.st_size
        final_mtime_ns = stat_after.st_mtime_ns
        if final_size != size or final_mtime_ns != mtime_ns:
            digest, second_read = calculate_compatible_sha1(abs_path)
            read_bytes += second_read
            stat_verify = os.stat(abs_path, follow_symlinks=False)
            if stat_verify.st_size != final_size or stat_verify.st_mtime_ns != final_mtime_ns:
                raise RuntimeError('文件在计算哈希期间持续发生变化，请关闭正在写入它的程序后重试')
            final_size = stat_verify.st_size
            final_mtime_ns = stat_verify.st_mtime_ns

        return rel, digest, read_bytes, final_size, final_mtime_ns

    if misses:
        print_color(f'需要重新计算哈希: {total_misses} 个文件（线程: {threads}）', Colors.YELLOW)
        with ThreadPoolExecutor(max_workers=threads, thread_name_prefix='sha1') as pool:
            future_map = {pool.submit(worker, item): item for item in misses}
            for future in as_completed(future_map):
                item = future_map[future]
                rel = item[0]
                try:
                    rel, digest, read_bytes, size, mtime_ns = future.result()
                    hashes[rel] = digest
                    new_cache[rel] = {
                        'size': size,
                        'mtimeNs': mtime_ns,
                        'hash': digest
                    }
                    bytes_read += read_bytes
                except Exception as e:
                    errors.append((rel, str(e)))
                completed += 1
                if completed == total_misses or completed % progress_step == 0:
                    pct = completed * 100.0 / total_misses
                    print(f'  哈希进度: {completed}/{total_misses} ({pct:.1f}%)', flush=True)

    return hashes, new_cache, cache_hits, len(misses), bytes_read, errors


def compare_manifests(old_manifest, new_hashes):
    old_files = {}
    if isinstance(old_manifest, dict):
        for item in old_manifest.get('files', []):
            if not isinstance(item, dict):
                continue
            path = item.get('path')
            digest = item.get('hash')
            if isinstance(path, str) and isinstance(digest, str):
                old_files[path] = digest.lower()

    new_paths = set(new_hashes)
    old_paths = set(old_files)
    added = sorted(new_paths - old_paths, key=str.casefold)
    deleted = sorted(old_paths - new_paths, key=str.casefold)
    common = new_paths & old_paths
    modified = sorted(
        [p for p in common if new_hashes[p].lower() != old_files[p]],
        key=str.casefold
    )
    unchanged = len(common) - len(modified)
    return added, modified, deleted, unchanged


def human_bytes(value):
    value = float(value)
    units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == 'B':
                return f'{int(value)} {unit}'
            return f'{value:.2f} {unit}'
        value /= 1024.0


def print_change_examples(title, paths, marker, color, limit=15):
    if not paths:
        return
    print()
    print_color(f'{title}（显示前 {min(limit, len(paths))}/{len(paths)}）:', color)
    for path in paths[:limit]:
        print(f'  {marker} {path}')
    if len(paths) > limit:
        print_color(f'  ... 还有 {len(paths) - limit} 个', Colors.GRAY)


def save_cache(cache_path, source_dir, new_cache):
    data = {
        'formatVersion': CACHE_FORMAT_VERSION,
        'hashRuleVersion': HASH_RULE_VERSION,
        'sourceDir': os.path.abspath(source_dir),
        'files': new_cache
    }
    atomic_write_json(cache_path, data)


# ============ 更新器发布：单一来源 -> 清单（玩家端自更新）============
#
# 更新器源码只有一份，默认放在 client/update/updater.py。导出时把它同步到
# 扫描目录下的 update/updater.py，于是它会作为普通文件进入清单；玩家端的更新器
# 校验到哈希不一致就会把它下载覆盖掉，下一次启动即生效——不需要额外机制。
#
# 想改更新器，只改那一份源文件即可，不要再直接改 overrides/update/updater.py
# 或玩家实例里的副本，那些都是被同步/覆盖的目标。

def read_updater_version(path):
    """从更新器源码里读出 UPDATER_VERSION，读不到返回 None。"""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except OSError:
        return None
    match = re.search(r"^UPDATER_VERSION\s*=\s*['\"]([^'\"]+)['\"]", content, re.MULTILINE)
    return match.group(1) if match else None


def resolve_updater_source(export_dir, config):
    """解析更新器源文件路径；配置为空字符串表示不发布更新器。"""
    raw = config.get('updaterSource', DEFAULT_UPDATER_SOURCE)
    raw = str(raw or '').strip()
    if not raw:
        return None
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(export_dir, raw))


def sync_updater(export_dir, source_dir, config):
    """把更新器源文件同步到扫描目录，使它能被清单发布出去。

    返回 (是否已就绪, 说明文本)。
    """
    target = os.path.join(source_dir, UPDATER_TARGET_REL.replace('/', os.sep))
    source = resolve_updater_source(export_dir, config)

    if source is None:
        print_color('更新器源未配置（updaterSource 为空），本次不发布更新器。', Colors.YELLOW)
        return os.path.exists(target), '已禁用'

    if not os.path.isfile(source):
        if os.path.exists(target):
            print_color(
                f'警告: 找不到更新器源文件 {source}，沿用已发布的 {UPDATER_TARGET_REL}。',
                Colors.YELLOW
            )
            return True, '沿用旧版'
        print_color(
            f'警告: 找不到更新器源文件 {source}，玩家端将无法自更新。', Colors.RED
        )
        return False, '缺失'

    with open(source, 'rb') as f:
        source_bytes = f.read()
    source_digest = hashlib.sha1(source_bytes).hexdigest()

    changed = True
    if os.path.isfile(target):
        with open(target, 'rb') as f:
            changed = hashlib.sha1(f.read()).hexdigest() != source_digest

    if changed:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as f:
            f.write(source_bytes)

    version = read_updater_version(source) or '未知'
    if changed:
        print_color(
            f'已同步更新器 v{version} -> {UPDATER_TARGET_REL}，玩家端会随普通更新自动升级。',
            Colors.GREEN
        )
    else:
        print_color(f'更新器 v{version} 已是最新，无需同步。', Colors.GREEN)
    return True, version


def resolve_client_manifest(export_dir, config):
    """解析客户端基线清单路径；配置为空字符串表示不管理。"""
    raw = config.get('clientManifest', DEFAULT_CLIENT_MANIFEST)
    raw = str(raw or '').strip()
    if not raw:
        return None
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(export_dir, raw))


def ensure_client_baseline(export_dir, config):
    """把客户端分发包里的基线清单版本号钉死在 1.0。

    这个文件会随客户端包覆盖玩家本地的清单，是所有玩家的统一起点。导出器
    只负责钉版本号，**不会**把新清单写进去——否则它就变成「当前版本」，
    老玩家那一步过时文件清理会失去依据。

    返回 (是否就绪, 说明文本)。
    """
    path = resolve_client_manifest(export_dir, config)
    if path is None:
        print_color('客户端基线清单未配置（clientManifest 为空），本次不检查。', Colors.YELLOW)
        return True, '已禁用'

    pinned = str(config.get('clientBaselineVersion', DEFAULT_CLIENT_BASELINE_VERSION) or '').strip()
    if not pinned:
        pinned = DEFAULT_CLIENT_BASELINE_VERSION

    if not os.path.isfile(path):
        print_color(f'提示: 未找到客户端基线清单 {path}，客户端包可能不完整。', Colors.YELLOW)
        return False, '缺失'

    data = load_json(path)
    if not isinstance(data, dict):
        print_color(f'警告: 客户端基线清单无法解析: {path}', Colors.RED)
        return False, '损坏'

    current = str(data.get('version', ''))
    files = data.get('files')
    file_count = len(files) if isinstance(files, list) else 0

    if current == pinned:
        print_color(f'客户端基线清单版本已是 {pinned}（{file_count} 个文件），保持不变。', Colors.GREEN)
        return True, pinned

    data['version'] = pinned
    try:
        atomic_write_json(path, data)
    except Exception as exc:
        print_color(f'警告: 无法写入客户端基线清单: {exc}', Colors.RED)
        return False, '写入失败'

    print_color(
        f'客户端基线清单版本已从 {current or "空"} 钉回 {pinned}（{file_count} 个文件）。',
        Colors.YELLOW
    )
    return True, pinned


# ============ 导出前检查：清单路径 vs git 索引大小写 ============
#
# 背景：Windows 的文件系统不区分大小写，但 Git 服务端（Gitea/Forgejo 的 raw
# 接口）按字节区分大小写。如果工作区里改过文件名大小写而没提交（或反过来，
# 索引里还是旧的大小写），清单里的路径在服务端就取不到，玩家下载会 404。
#
# 处理方式：不要求作者去改仓库里的文件名，而是在清单条目上附加一个可选的
# urlPath 字段，记录该文件在仓库里的真实大小写。更新器拼下载地址时优先用
# urlPath，落地到本地时仍用 path（磁盘上的大小写），两边各取所需。
#
# 这样：
# - 仓库保持原样也能正常发布，玩家不受影响；
# - 作者哪天顺手把仓库改名改一致了，下一份清单就不会再有 urlPath，自动收敛。
#
# 清单里的 path 始终用磁盘上的真实大小写，所以相邻两次导出之间不会因为
# 大小写来回翻转而产生假的新增/删除。

def run_git(args, cwd, timeout=120):
    """执行 git 命令，返回 (返回码或 None, 标准输出字节, 错误说明)。"""
    try:
        completed = subprocess.run(
            ['git', *args],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout
        )
    except FileNotFoundError:
        return None, b'', '未找到 git 命令'
    except Exception as exc:
        return None, b'', f'{type(exc).__name__}: {exc}'

    stderr = completed.stderr.decode('utf-8', errors='replace').strip()
    return completed.returncode, completed.stdout, stderr


def resolve_repo_prefix(export_dir, source_dir):
    """定位 git 仓库根目录和扫描目录在仓库内的相对前缀。

    返回 (仓库根目录, 前缀, 说明)。仓库不可用时前缀为 None。
    """
    code, out, err = run_git(['rev-parse', '--show-toplevel'], export_dir)
    if code != 0:
        return None, None, err or '当前目录不是 git 仓库'

    repo_root = out.decode('utf-8', errors='replace').strip().replace('\\', '/')
    if not repo_root:
        return None, None, '无法解析 git 仓库根目录'
    if not path_is_inside(repo_root, source_dir):
        return repo_root, None, '扫描目录不在该 git 仓库内'

    rel = os.path.relpath(os.path.abspath(source_dir), os.path.abspath(repo_root))
    rel = rel.replace('\\', '/')
    return repo_root, ('' if rel == '.' else rel), ''


def load_git_index_paths(repo_root, prefix):
    """读取仓库索引中该前缀下的全部文件路径（相对前缀，保留真实大小写）。"""
    code, out, err = run_git(['ls-files', '-z', '--', prefix or '.'], repo_root)
    if code != 0:
        return None, err or 'git ls-files 执行失败'

    head = f'{prefix}/' if prefix else ''
    result = set()
    for raw in out.split(b'\0'):
        if not raw:
            continue
        path = raw.decode('utf-8', errors='surrogateescape').replace('\\', '/')
        if head:
            if not path.startswith(head):
                continue
            path = path[len(head):]
        result.add(path)
    return result, ''


def check_repo_case_consistency(export_dir, source_dir, rel_paths):
    """比对清单路径与 git 索引。

    返回 (报告字典, 说明)。仓库不可用时报告为 None。
    报告包含：
      case_mismatch  —— 仅大小写不同（导出时写入 urlPath，玩家不受影响）
      repo_missing   —— 索引里完全没有（提交前玩家下载不到）
      stale_in_repo  —— 索引里有但磁盘上确实没有（大小写差异不算）
    """
    repo_root, prefix, reason = resolve_repo_prefix(export_dir, source_dir)
    if prefix is None:
        return None, reason

    index_paths, reason = load_git_index_paths(repo_root, prefix)
    if index_paths is None:
        return None, reason

    index_lower = {}
    for path in index_paths:
        index_lower.setdefault(path.lower(), path)

    case_mismatch = []
    repo_missing = []
    for rel in rel_paths:
        if rel in index_paths:
            continue
        real = index_lower.get(rel.lower())
        if real:
            case_mismatch.append((rel, real))
        else:
            repo_missing.append(rel)

    # 磁盘存在性按大小写不敏感判断：仅大小写不同不算“已删除”，
    # 否则同一批路径会同时出现在 case_mismatch 和 stale_in_repo 里，纯属噪音。
    on_disk_lower = {path.lower() for path in rel_paths}
    stale_in_repo = sorted(
        (path for path in index_paths if path.lower() not in on_disk_lower),
        key=str.casefold
    )
    return {
        'repo_root': repo_root,
        'prefix': prefix,
        'case_mismatch': case_mismatch,
        'repo_missing': repo_missing,
        'stale_in_repo': stale_in_repo,
    }, ''


def build_url_case_map(export_dir, source_dir, rel_paths):
    """找出磁盘大小写与 git 索引不一致的路径。

    返回 (映射, 报告, 说明)。映射形如 {清单路径: 仓库真实路径}，只包含两者
    大小写不同的条目；仓库不可用时映射为空、报告为 None。
    """
    report, reason = check_repo_case_consistency(export_dir, source_dir, rel_paths)
    if report is None:
        return {}, None, reason
    return {
        manifest_path: real_path for manifest_path, real_path in report['case_mismatch']
    }, report, ''


def build_file_entry(rel_path, digest, url_case_map):
    """构造清单条目；仅当仓库真实大小写与清单不同时才附加 urlPath。"""
    entry = {'path': rel_path, 'hash': digest}
    real_path = url_case_map.get(rel_path)
    if real_path and real_path != rel_path:
        entry['urlPath'] = real_path
    return entry


def report_case_findings(url_case_map, report, reason):
    """打印检查结果。

    不阻断导出：大小写差异由清单里的 urlPath 兜住，更新器会按仓库真实大小写
    取文件，玩家侧不受影响，因此仓库里的文件名不需要为了发布去改。
    """
    print()
    print_color('检查清单路径与 git 索引大小写...', Colors.YELLOW)

    if report is None:
        print_color(f'已跳过检查（{reason}）。', Colors.GRAY)
        return

    if url_case_map:
        print_color(
            f'发现 {len(url_case_map)} 个路径仅大小写与 git 索引不同，'
            f'已在清单中写入 urlPath，更新器会按仓库真实大小写下载，玩家不受影响。',
            Colors.YELLOW
        )
        for manifest_path, real_path in list(url_case_map.items())[:5]:
            print(f'    - 清单: {manifest_path}')
            print(f'      仓库: {real_path}')
        if len(url_case_map) > 5:
            print_color(f'    ... 还有 {len(url_case_map) - 5} 个', Colors.GRAY)
    else:
        print_color('大小写检查通过：所有路径与 git 索引一致。', Colors.GREEN)

    repo_missing = report['repo_missing']
    if repo_missing:
        print()
        print_color(
            f'提示: 有 {len(repo_missing)} 个文件还不在 git 索引里（通常是本次新增、尚未提交）。',
            Colors.YELLOW
        )
        for path in repo_missing[:10]:
            print(f'    - {path}')
        if len(repo_missing) > 10:
            print_color(f'    ... 还有 {len(repo_missing) - 10} 个', Colors.GRAY)
        print_color('  这些文件在提交前玩家下载不到，记得 git add 并推送。', Colors.YELLOW)

    stale_in_repo = report['stale_in_repo']
    if stale_in_repo:
        print()
        print_color(
            f'提示: 有 {len(stale_in_repo)} 个文件仍在 git 索引里，但磁盘上已经没有了。',
            Colors.YELLOW
        )
        for path in stale_in_repo[:10]:
            print(f'    - {path}')
        if len(stale_in_repo) > 10:
            print_color(f'    ... 还有 {len(stale_in_repo) - 10} 个', Colors.GRAY)
        print_color('  它们会被继续发布给玩家；如果确实该删，请 git rm 后提交。', Colors.YELLOW)


def warn_uncommitted_changes(export_dir, source_dir):
    """提醒作者：仓库里还有未提交的改动，玩家要等 push 之后才能拿到。"""
    repo_root, prefix, _ = resolve_repo_prefix(export_dir, source_dir)
    if prefix is None:
        return

    code, out, _ = run_git(['status', '--porcelain', '--', prefix or '.'], repo_root)
    if code != 0 or not out.strip():
        return

    changed = [line for line in out.decode('utf-8', errors='replace').splitlines() if line.strip()]
    print()
    print_color(
        f'提醒: {prefix}/ 下还有 {len(changed)} 项未提交的改动，玩家要等你 commit & push 之后才能下载到。',
        Colors.YELLOW
    )


# ============ 已废弃文件（removedFiles）============
# 背景：更新器删除过时文件时，若只做「客户端本地清单 − 新清单」的差集，那么
# 客户端起点版本就决定了他能删掉什么 —— 跨版本升级时，中间版本加进来又删掉的
# 文件永远删不掉（实测：v1.0 的客户端直更到 v1.9，v1.4 时代的旧 jar 一个都删不掉）。
#
# 所以改由导出器把「曾经在包里出现过、现在不要了」的路径累积成 removedFiles 写进
# 清单。更新器照单删除，与客户端起点版本无关，也不会误删玩家自己加的文件。
#
# 累积规则：
#   removedFiles = 上一份清单的 removedFiles
#                ∪ (上一份清单的 files − 本次清单的 files)
#                − 本次清单的 files      # 重新加回包里的，撤销废弃
# 首次运行（旧清单里没有该字段）会从 git 历史里把历次清单的路径并起来补种，
# 这样已经漂移的存量客户端也能被一次清理干净。

REMOVED_FILES_FIELD = 'removedFiles'


def seed_paths_from_git_history(export_dir, manifest_path):
    """并出 git 历史里历次清单出现过的全部文件路径（仅首次补种用）。"""
    repo_root, _, reason = resolve_repo_prefix(export_dir, export_dir)
    if repo_root is None:
        print_color(f'  跳过历史补种（{reason}）', Colors.GRAY)
        return set()

    manifest_rel = os.path.relpath(
        os.path.abspath(manifest_path), os.path.abspath(repo_root)
    ).replace('\\', '/')

    code, out, err = run_git(['rev-list', '--all', '--', manifest_rel], repo_root, timeout=60)
    if code != 0:
        print_color(f'  跳过历史补种（{err or "git rev-list 执行失败"}）', Colors.GRAY)
        return set()

    revisions = [line.strip() for line in out.decode('utf-8', errors='replace').splitlines() if line.strip()]
    paths = set()
    for revision in revisions:
        code, blob, _ = run_git(['show', f'{revision}:{manifest_rel}'], repo_root, timeout=120)
        if code != 0 or not blob:
            continue
        try:
            data = json.loads(blob.decode('utf-8', errors='replace'))
        except ValueError:
            continue
        for item in data.get('files') or []:
            path = item.get('path') if isinstance(item, dict) else None
            if path:
                paths.add(path.replace('\\', '/'))
    return paths


def normalize_manifest_path(path):
    """用于「是否同一个文件」的比较：统一分隔符 + 大小写折叠。

    Windows 文件系统不区分大小写，历史清单里可能记录的是 `[ThaumcraftMobAspects]`
    而磁盘/当前清单是 `[thaumcraftmobaspects]`。若按字节比对，这种纯大小写差异会被
    误判成「文件已被删除」写进废弃清单 —— 更新器在 Windows 上会把刚下好的文件删掉，
    来回死循环。所以一切集合运算都必须走这个归一化。
    """
    return path.replace('\\', '/').casefold()


def collect_removed_files(export_dir, manifest_path, old_manifest, new_paths):
    """算出本次要发布的「已废弃文件」清单，返回 (排序后的列表, 是否做了历史补种)。"""
    new_keys = {normalize_manifest_path(p) for p in new_paths}

    removed = {
        path.replace('\\', '/')
        for path in (old_manifest.get(REMOVED_FILES_FIELD) or [])
        if isinstance(path, str) and path and normalize_manifest_path(path) not in new_keys
    }

    old_paths = {
        item.get('path', '').replace('\\', '/')
        for item in (old_manifest.get('files') or [])
        if isinstance(item, dict) and item.get('path')
    }
    removed |= {p for p in old_paths if normalize_manifest_path(p) not in new_keys}

    seeded = False
    if REMOVED_FILES_FIELD not in old_manifest:
        historical = seed_paths_from_git_history(export_dir, manifest_path)
        if historical:
            removed |= {p for p in historical if normalize_manifest_path(p) not in new_keys}
            seeded = True
            print_color(f'  首次生成：已从 git 历史并出 {len(historical)} 条历史路径用于补种', Colors.GRAY)

    # 重新加回包里的文件撤销废弃（大小写不敏感），否则更新器会「删了又下」。
    removed = {p for p in removed if normalize_manifest_path(p) not in new_keys}
    removed.discard('')
    return sorted(removed, key=str.casefold), seeded


def main():
    total_start = time.perf_counter()
    script_dir = os.path.dirname(os.path.abspath(__file__))

    print_color('=' * 62, Colors.CYAN)
    print_color('  Minecraft 整合包 Manifest / SHA-1 导出工具', Colors.CYAN)
    print_color('=' * 62, Colors.CYAN)
    print()

    config, config_path, _ = setup_or_load_config(script_dir)
    source_dir = normalize_user_path(config['sourceDir'])
    file_api = normalize_file_api(config['fileApi'])
    export_dir = _derive_export_dir_from_legacy_config(script_dir, config)
    threads = sanitize_threads(config.get('hashThreads', DEFAULT_HASH_THREADS))
    manifest_path = os.path.abspath(manifest_path_from_config(script_dir, config))
    cache_path = os.path.abspath(cache_path_from_config(script_dir, config))

    old_manifest = load_json(manifest_path)
    if not isinstance(old_manifest, dict):
        # 兼容旧版本把清单放在脚本目录或扫描目录的情况。
        _, candidate = find_existing_manifest(script_dir, source_dir, export_dir)
        if isinstance(candidate, dict):
            old_manifest = candidate

    if not isinstance(old_manifest, dict):
        # 兼容首次把旧清单放在扫描目录的情况
        source_manifest_path = os.path.join(source_dir, DEFAULT_MANIFEST_FILE)
        if norm_abs(source_manifest_path) != norm_abs(manifest_path):
            candidate = load_json(source_manifest_path)
            if isinstance(candidate, dict):
                old_manifest = candidate
                print_color(f'已读取扫描目录中的旧清单: {source_manifest_path}', Colors.GRAY)

    name, author, current_version, description, addons = get_manifest_metadata(old_manifest, source_dir)

    print_color(f'整合包名称: {name}', Colors.GREEN)
    if author:
        print_color(f'作者: {author}', Colors.GREEN)
    print_color(f'扫描目录: {source_dir}', Colors.GREEN)
    print_color(f'更新地址: {file_api}', Colors.GREEN)
    print_color(f'导出目录: {export_dir}', Colors.GREEN)
    print_color(f'输出清单: {manifest_path}', Colors.GREEN)
    print_color(f'哈希线程: {threads}', Colors.GREEN)
    print()

    new_version = prompt_new_version(current_version)
    print_color(f'本次导出版本: {new_version}', Colors.CYAN)
    print()

    # 先把更新器同步进扫描目录，让它作为普通文件进入清单，实现玩家端自更新。
    sync_updater(export_dir, source_dir, config)

    # 客户端基线清单固定 1.0：随包发给玩家、覆盖其本地清单，保证统一起点。
    # 注意这里只钉版本号，绝不把新清单写进客户端分发包。
    ensure_client_baseline(export_dir, config)

    excluded = build_excluded_paths(script_dir, source_dir, config_path, manifest_path, cache_path)

    scan_start = time.perf_counter()
    print_color('扫描文件...', Colors.YELLOW)
    try:
        scanned_files = scan_source_files(source_dir, excluded)
    except Exception as e:
        print_color(f'错误: {e}', Colors.RED)
        return 1
    scan_elapsed = time.perf_counter() - scan_start

    total_size = sum(item[2] for item in scanned_files)
    print_color(
        f'扫描完成: {len(scanned_files)} 个文件，{human_bytes(total_size)}，用时 {scan_elapsed:.2f} 秒',
        Colors.GREEN
    )

    # 大小写检查放在哈希之前，结果用来给清单附加 urlPath；不阻断导出。
    url_case_map, case_report, case_reason = build_url_case_map(
        export_dir, source_dir, [rel for rel, _, _, _ in scanned_files]
    )
    report_case_findings(url_case_map, case_report, case_reason)

    cache_files = load_hash_cache(cache_path, source_dir)
    hash_start = time.perf_counter()
    hashes, new_cache, cache_hits, rehashed, bytes_read, hash_errors = hash_files_with_cache(
        scanned_files, cache_files, threads
    )
    hash_elapsed = time.perf_counter() - hash_start

    if hash_errors:
        print()
        print_color(f'有 {len(hash_errors)} 个文件无法计算哈希，本次不覆盖旧清单。', Colors.RED)
        for rel, error in hash_errors[:20]:
            print(f'  - {rel}: {error}')
        if len(hash_errors) > 20:
            print(f'  ... 还有 {len(hash_errors) - 20} 个')
        return 1

    # 扫描顺序已经稳定排序；直接按它构造输出，避免再对 1 万多项做一次排序。
    files_output = [
        build_file_entry(rel, hashes[rel], url_case_map)
        for rel, _, _, _ in scanned_files
    ]

    added, modified, deleted, unchanged = compare_manifests(old_manifest, hashes)

    # 已废弃文件清单：累积发布，更新器照单删除，与客户端起点版本无关。
    removed_files, _ = collect_removed_files(export_dir, manifest_path, old_manifest, set(hashes))
    new_removed = [p for p in removed_files if p not in (old_manifest.get(REMOVED_FILES_FIELD) or [])]

    new_manifest = {
        'name': name,
        'author': author,
        'version': new_version,
        'description': description,
        'fileApi': file_api,
        'files': files_output,
        REMOVED_FILES_FIELD: removed_files,
        'addons': addons
    }

    # 只有所有文件都成功后才写入缓存和正式清单。
    # 完全命中且缓存没有陈旧路径时不重写缓存文件，减少 1 万+ 文件场景下的额外磁盘写入。
    cache_unchanged = (
        rehashed == 0
        and cache_hits == len(scanned_files)
        and len(cache_files) == len(new_cache)
    )
    if not cache_unchanged:
        save_cache(cache_path, source_dir, new_cache)
    atomic_write_json(manifest_path, new_manifest)

    # 把最终规范值回写配置，方便以后调整线程等配置时仍保持完整。
    config['sourceDir'] = source_dir
    config['fileApi'] = file_api
    config['exportDir'] = export_dir
    config['hashThreads'] = threads
    config['manifestFile'] = os.path.basename(manifest_path)
    config['hashCacheFile'] = os.path.relpath(cache_path, script_dir) if path_is_inside(script_dir, cache_path) else cache_path
    config['updaterSource'] = str(config.get('updaterSource', DEFAULT_UPDATER_SOURCE) or '')
    config['clientManifest'] = str(config.get('clientManifest', DEFAULT_CLIENT_MANIFEST) or '')
    config['clientBaselineVersion'] = str(
        config.get('clientBaselineVersion', DEFAULT_CLIENT_BASELINE_VERSION) or ''
    )
    atomic_write_json(config_path, config)

    total_elapsed = time.perf_counter() - total_start

    print()
    print_color('=' * 62, Colors.CYAN)
    print_color('  导出完成', Colors.CYAN)
    print_color('=' * 62, Colors.CYAN)
    print(f'整合包:       {name}')
    print(f'版本:         {current_version} -> {new_version}')
    print(f'总文件数:     {len(files_output)}')
    print_color(f'新增:         {len(added)}', Colors.GREEN)
    print_color(f'修改:         {len(modified)}', Colors.YELLOW)
    print_color(f'删除:         {len(deleted)}', Colors.RED if deleted else Colors.RESET)
    print(f'未变化:       {unchanged}')
    print(f'缓存复用:     {cache_hits}')
    print(f'重新计算哈希: {rehashed}')
    print(f'实际读取数据: {human_bytes(bytes_read)}')
    print(f'扫描耗时:     {scan_elapsed:.2f} 秒')
    print(f'哈希耗时:     {hash_elapsed:.2f} 秒')
    print(f'总耗时:       {total_elapsed:.2f} 秒')
    print(f'输出文件:     {manifest_path}')
    print(f'哈希缓存:     {cache_path}')
    print_color(f'废弃清单:     {len(removed_files)} 条（本次新增 {len(new_removed)}）', Colors.CYAN)

    print_change_examples('新增文件', added, '+', Colors.GREEN)
    print_change_examples('修改文件', modified, '~', Colors.YELLOW)
    print_change_examples('删除文件', deleted, '-', Colors.RED)
    print_change_examples('新废弃（更新器会删掉客户端上的这些）', new_removed, 'x', Colors.RED)

    print()
    if not added and not modified and not deleted:
        print_color('文件内容没有变化；本次只更新了版本号/清单元数据。', Colors.GREEN)
    else:
        print_color(
            f'本次文件变更合计: {len(added) + len(modified) + len(deleted)} '
            f'(新增 {len(added)} / 修改 {len(modified)} / 删除 {len(deleted)})',
            Colors.CYAN
        )

    warn_uncommitted_changes(export_dir, source_dir)

    return 0


def run():
    exit_code = 1
    try:
        exit_code = main()
    except KeyboardInterrupt:
        print()
        print_color('用户取消操作。', Colors.YELLOW)
        exit_code = 130
    except Exception as e:
        print()
        print_color(f'未处理的错误: {e}', Colors.RED)
        exit_code = 1

    if sys.stdin.isatty():
        print()
        try:
            input('按回车键退出...')
        except (EOFError, KeyboardInterrupt):
            pass
    return exit_code


if __name__ == '__main__':
    sys.exit(run())
