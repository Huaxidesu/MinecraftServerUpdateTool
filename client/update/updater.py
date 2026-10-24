# updater.py
import os
import sys
import json
import hashlib
import re
import socket
import threading
import urllib.error
import urllib.request
import urllib.parse
import shutil
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---- 配置区 ----

UPDATER_VERSION = '2.4'

# 更新器自身在整合包目录里的相对路径。它也会出现在清单里，
# 于是玩家端能通过普通更新流程把自己升级掉。
UPDATER_REL_PATH = 'update/updater.py'

MANIFEST_FILE = 'server-manifest.json'
EXTRA_CONFIG_FILE = 'server-extra.json'
MINECRAFT_DIR_NAME = '.minecraft'
UPDATER_CONFIG_FILE = 'updater-config.json'
DEFAULT_DOWNLOAD_THREADS = 5
MAX_DOWNLOAD_THREADS = 16

# 单个请求的超时时间（秒）。旧版本没有超时，连接卡死会一直挂着。
DOWNLOAD_TIMEOUT = 60
# 单个文件的最大尝试次数（含首次）。仅对网络类/服务端 5xx 错误重试。
MAX_DOWNLOAD_ATTEMPTS = 3
# 重试退避基数（秒），第 n 次重试前等待 RETRY_BACKOFF_SECONDS * n。
RETRY_BACKOFF_SECONDS = 1.5
# 部分服务器会拒绝默认的 Python UA，显式声明一个。
USER_AGENT = f'GTNH-Pack-Updater/{UPDATER_VERSION}'

# 从 fileApi 里解析出仓库坐标，用于 404 时反查真实路径大小写。
# 形如 https://host/owner/repo/raw/branch/main/
RAW_FILE_API_RE = re.compile(
    r'^(?P<origin>https?://[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)'
    r'/raw/(?:branch|commit|tag)/(?P<ref>[^/]+)/*$'
)

# 二进制文件扩展名
BINARY_EXTENSIONS = {
    '.jar', '.zip', '.png', '.jpg', '.jpeg', '.gif', '.bmp',
    '.ico', '.ttf', '.otf', '.woff', '.woff2', '.mp3', '.ogg',
    '.wav', '.mp4', '.avi', '.mov', '.class', '.dat', '.bin'
}

# ANSI 颜色代码
class Colors:
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    GRAY = '\033[90m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

# Windows 下启用 ANSI 颜色支持
if sys.platform == 'win32':
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        # 如果失败，禁用颜色
        Colors.CYAN = Colors.GREEN = Colors.YELLOW = Colors.RED = Colors.GRAY = Colors.RESET = Colors.BOLD = ''

# ---- 工具函数 ----

def print_color(text, color=Colors.RESET):
    """彩色打印"""
    print(f"{color}{text}{Colors.RESET}")

def normalize_user_path(path):
    """清理玩家粘贴的路径，兼容首尾引号和环境变量。"""
    path = str(path or '').strip().strip('"').strip("'")
    path = os.path.expandvars(os.path.expanduser(path))
    return os.path.abspath(path) if path else ''


def validate_minecraft_path(path):
    """验证目标目录是否像整合包目录：至少需要同时存在 config 和 mods 文件夹。"""
    normalized = normalize_user_path(path)

    if not normalized:
        return False, normalized, '路径不能为空'

    if not os.path.isdir(normalized):
        return False, normalized, '该路径不存在，或不是文件夹'

    missing_dirs = []
    for required_dir in ('config', 'mods'):
        if not os.path.isdir(os.path.join(normalized, required_dir)):
            missing_dirs.append(required_dir)

    if missing_dirs:
        return False, normalized, f"缺少关键文件夹: {', '.join(missing_dirs)}"

    return True, normalized, ''


def save_updater_config(config_path, config):
    """保存更新器本地持久化配置。"""
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def prompt_minecraft_path():
    """首次运行或原路径失效时，引导玩家输入整合包实际目录。"""
    print_color('需要设置整合包目录。', Colors.CYAN)
    print('请粘贴包含 config 和 mods 文件夹的整合包目录。')
    print_color('例如：', Colors.GRAY)
    print(r'  D:\Game\Minecraft\HMCL\.minecraft\versions\GT New Horizons 2.8.3')
    print(r'  D:\Game\Minecraft\MMCproject\instances\GT_New_Horizons_2.9.0-beta-2_Java_17-25\.minecraft')
    print()

    while True:
        raw_path = input('整合包路径: ').strip()
        valid, normalized, reason = validate_minecraft_path(raw_path)
        if valid:
            print_color(f'目录验证成功: {normalized}', Colors.GREEN)
            return normalized

        print_color(f'目录验证失败: {reason}', Colors.RED)
        print('请确认你粘贴的是实际游戏目录，并且其中同时存在 config 和 mods 文件夹。')
        print()


def load_or_create_updater_config(script_dir):
    """
    加载更新器本地持久化配置。
    首次运行只询问整合包路径；下载线程数直接使用默认值 5。
    """
    config_path = os.path.join(script_dir, UPDATER_CONFIG_FILE)
    first_run = not os.path.exists(config_path)
    config = {}
    config_changed = False

    if not first_run:
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            if not isinstance(config, dict):
                raise ValueError('配置文件根节点必须是对象')
        except Exception as e:
            print_color('警告: 更新器设置已损坏，将重新创建。', Colors.YELLOW)
            config = {}
            first_run = True

    if first_run:
        print_color('第一次使用，需要先设置整合包目录。', Colors.CYAN)
        print_color(f'下载线程数使用默认值 {DEFAULT_DOWNLOAD_THREADS}。', Colors.GRAY)
        minecraft_path = prompt_minecraft_path()
        config = {
            'minecraftPath': minecraft_path,
            'updaterVersion': UPDATER_VERSION,
            'downloadThreads': DEFAULT_DOWNLOAD_THREADS,
            # 排查问题时改成 true，会多打印大小写校正等诊断明细。
            'verbose': False,
            # 想永久保留的文件（比如自己额外装的 mod），按路径或通配符写在这里，
            # 更新时不会被清理。留空即可。
            'keepFiles': []
        }
        save_updater_config(config_path, config)
        print_color('设置已保存。', Colors.GREEN)
        print()
        return config

    # 验证已有 Minecraft 路径；如果失效，重新询问并持久化。
    valid, normalized_path, reason = validate_minecraft_path(config.get('minecraftPath', ''))
    if not valid:
        print_color(f'已保存的整合包路径无效: {reason}', Colors.YELLOW)
        normalized_path = prompt_minecraft_path()
        config['minecraftPath'] = normalized_path
        config_changed = True
    elif config.get('minecraftPath') != normalized_path:
        config['minecraftPath'] = normalized_path
        config_changed = True

    # 下载线程数不在首次运行时询问，默认 5；允许玩家以后手动编辑配置，最高 16。
    raw_threads = config.get('downloadThreads', DEFAULT_DOWNLOAD_THREADS)
    try:
        download_threads = int(raw_threads)
    except (TypeError, ValueError):
        download_threads = DEFAULT_DOWNLOAD_THREADS
        print_color(f'警告: 下载线程数设置无效，已恢复为 {DEFAULT_DOWNLOAD_THREADS}。', Colors.YELLOW)
        config_changed = True

    if download_threads < 1:
        print_color(f'警告: 下载线程数不能小于 1，已恢复为 {DEFAULT_DOWNLOAD_THREADS}。', Colors.YELLOW)
        download_threads = DEFAULT_DOWNLOAD_THREADS
        config_changed = True
    elif download_threads > MAX_DOWNLOAD_THREADS:
        print_color(f'警告: 下载线程数最高为 {MAX_DOWNLOAD_THREADS}，已自动限制。', Colors.YELLOW)
        download_threads = MAX_DOWNLOAD_THREADS
        config_changed = True

    # verbose 默认关闭：诊断明细只给打包方排查用，不打扰玩家。
    if 'verbose' not in config:
        config['verbose'] = False
        config_changed = True

    if config.get('downloadThreads') != download_threads:
        config['downloadThreads'] = download_threads
        config_changed = True

    # 记录本次运行的更新器版本，方便排查「玩家在用多旧的更新器」。
    if config.get('updaterVersion') != UPDATER_VERSION:
        config['updaterVersion'] = UPDATER_VERSION
        config_changed = True

    if config_changed:
        try:
            save_updater_config(config_path, config)
        except Exception as e:
            print_color('警告: 更新器设置保存失败，下次启动会重新设置。', Colors.YELLOW)

    return config


def load_extra_config(root_dir, file_api):
    """
    加载额外配置文件，返回配置对象和不覆盖的文件列表
    如果服务器有新版本，自动更新
    """
    config_path = os.path.join(root_dir, EXTRA_CONFIG_FILE)
    
    # 默认配置
    default_config = {
        "version": "1",
        "noOverwriteFiles": [
            "options.txt",
            "hotbar.nbt"
        ]
    }
    
    # 读取本地配置
    local_config = None
    local_version = "0"
    
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                local_config = json.load(f)
            local_version = str(local_config.get("version", "0"))
        except Exception as e:
            print_color('警告: 附加设置无法读取，将使用默认值。', Colors.YELLOW)
            local_config = None
    
    # 尝试从服务器下载配置
    server_config = None
    server_version = "0"
    
    if file_api:
        try:
            server_config_url = f'{file_api}/{EXTRA_CONFIG_FILE}'
            script_dir = os.path.dirname(os.path.abspath(__file__))
            temp_config_path = os.path.join(script_dir, '.server-extra-temp.json')
            
            if download_file(server_config_url, temp_config_path, show_error=False)[0]:
                with open(temp_config_path, 'r', encoding='utf-8') as f:
                    server_config = json.load(f)
                server_version = str(server_config.get("version", "0"))
                
                # 清理临时文件
                try:
                    os.remove(temp_config_path)
                except Exception:
                    pass
        except Exception:
            pass
    
    # 决定使用哪个配置
    if server_config and server_version > local_version:
        # 服务器有新版本，更新本地
        print_color(f'附加设置已更新: {local_version} -> {server_version}', Colors.CYAN)
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(server_config, f, ensure_ascii=False, indent=2)
            print_color('附加设置已更新。', Colors.GREEN)
            return server_config, server_config.get("noOverwriteFiles", [])
        except Exception as e:
            print_color('警告: 附加设置保存失败，本次更新仍会继续。', Colors.YELLOW)
            return server_config, server_config.get("noOverwriteFiles", [])
    
    elif local_config:
        # 使用本地配置
        return local_config, local_config.get("noOverwriteFiles", [])
    
    else:
        # 创建默认配置
        print_color('未找到附加设置，已创建默认设置。', Colors.YELLOW)
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print_color('警告: 附加设置创建失败，本次更新仍会继续。', Colors.YELLOW)
        
        return default_config, default_config["noOverwriteFiles"]

def is_binary_file(file_path):
    """判断文件是否为二进制文件"""
    ext = os.path.splitext(file_path)[1].lower()
    return ext in BINARY_EXTENSIONS

import fnmatch

def is_no_overwrite_file(file_path, no_overwrite_files):
    """判断是否是不覆盖的文件，支持通配符"""
    normalized_path = file_path.replace('\\', '/')
    
    for pattern in no_overwrite_files:
        pattern_normalized = pattern.replace('\\', '/')
        
        # 如果配置以 / 结尾，视为目录匹配
        if pattern_normalized.endswith('/'):
            if normalized_path.startswith(pattern_normalized):
                return True
        
        # 使用 fnmatch 支持通配符 * 和 ?
        elif fnmatch.fnmatch(normalized_path, pattern_normalized):
            return True
        
        # 如果配置中没有 /，则只匹配根目录文件
        elif '/' not in pattern_normalized and '/' not in normalized_path:
            if fnmatch.fnmatch(os.path.basename(normalized_path), pattern_normalized):
                return True
    
    return False

def process_force_delete(extra_config, mc_dir):
    """处理强制删除的文件"""
    import fnmatch
    
    force_delete = extra_config.get("forceDelete", [])
    if not force_delete:
        return 0
    
    deleted_count = 0
    
    print_color('检查需要删除的文件...', Colors.YELLOW)
    
    for pattern in force_delete:
        pattern_normalized = pattern.replace('\\', '/')
        
        # 如果是目录模式
        if pattern_normalized.endswith('/'):
            target_dir = os.path.join(mc_dir, pattern_normalized.rstrip('/'))
            if os.path.exists(target_dir) and os.path.isdir(target_dir):
                print_color(f'  删除目录: {pattern}', Colors.YELLOW)
                shutil.rmtree(target_dir)
                deleted_count += 1
        
        # 如果包含通配符，需要遍历查找
        elif '*' in pattern_normalized or '?' in pattern_normalized:
            # 获取目录和文件名模式
            dir_part = os.path.dirname(pattern_normalized)
            file_pattern = os.path.basename(pattern_normalized)
            
            search_dir = os.path.join(mc_dir, dir_part) if dir_part else mc_dir
            
            if os.path.exists(search_dir):
                for filename in os.listdir(search_dir):
                    if fnmatch.fnmatch(filename, file_pattern):
                        file_path = os.path.join(search_dir, filename)
                        if os.path.isfile(file_path):
                            relative_path = os.path.relpath(file_path, mc_dir).replace('\\', '/')
                            print_color(f'  删除文件: {relative_path}', Colors.YELLOW)
                            remove_readonly(file_path)
                            os.remove(file_path)
                            deleted_count += 1
        
        # 精确匹配
        else:
            file_path = os.path.join(mc_dir, pattern_normalized)
            if os.path.exists(file_path):
                if os.path.isfile(file_path):
                    print_color(f'  删除文件: {pattern}', Colors.YELLOW)
                    remove_readonly(file_path)
                    os.remove(file_path)
                    deleted_count += 1
                elif os.path.isdir(file_path):
                    print_color(f'  删除目录: {pattern}', Colors.YELLOW)
                    shutil.rmtree(file_path)
                    deleted_count += 1
    
    return deleted_count

def calculate_file_hash(file_path):
    """
    计算文件哈希值
    对于文本文件，统一换行符为 LF 后再计算
    对于二进制文件，直接计算原始内容
    """
    try:
        with open(file_path, 'rb') as f:
            file_bytes = f.read()
        
        # 如果是二进制文件，直接计算哈希
        if is_binary_file(file_path):
            return hashlib.sha1(file_bytes).hexdigest()
        
        # 文本文件：尝试转换换行符
        try:
            text_content = file_bytes.decode('utf-8')
            normalized_content = text_content.replace('\r\n', '\n')
            normalized_bytes = normalized_content.encode('utf-8')
            return hashlib.sha1(normalized_bytes).hexdigest()
        except UnicodeDecodeError:
            # 如果无法解码为文本，按二进制处理
            return hashlib.sha1(file_bytes).hexdigest()
    
    except Exception as e:
        return None

def remove_readonly(file_path):
    """移除文件的只读属性"""
    if os.path.exists(file_path):
        try:
            os.chmod(file_path, 0o666)
        except Exception:
            pass

def describe_download_error(exc):
    """把底层异常翻译成玩家看得懂、作者能定位的说明。"""
    if isinstance(exc, urllib.error.HTTPError):
        return f'HTTP {exc.code} {exc.reason}'
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, socket.timeout):
            return f'网络超时（超过 {DOWNLOAD_TIMEOUT} 秒无响应）'
        return f'网络错误: {reason}'
    if isinstance(exc, socket.timeout):
        return f'网络超时（超过 {DOWNLOAD_TIMEOUT} 秒无响应）'
    return f'{type(exc).__name__}: {exc}'


def http_status_of(detail):
    """从失败说明里取出 HTTP 状态码，取不到返回 None。"""
    match = re.match(r'^HTTP (\d{3})', detail or '')
    return int(match.group(1)) if match else None


def is_retryable_failure(detail):
    """网络类错误和服务端 5xx 值得重试；4xx 是确定性失败，重试没有意义。"""
    status = http_status_of(detail)
    if status is None:
        return True
    return status >= 500


def friendly_download_error(detail, verbose=False):
    """把内部失败说明翻译成玩家看得懂的一句话。

    内部 detail 仍是 describe_download_error() 的原样（http_status_of 依赖它），
    这里只负责「给玩家看什么」；原始说明仅在 verbose 时附在括号里，供打包方排查。
    """
    status = http_status_of(detail)

    if status == 404:
        text = '服务器上没有这个文件'
    elif status in (401, 403):
        text = '服务器拒绝访问这个文件'
    elif status == 429:
        text = '服务器太忙，请稍后再试'
    elif status is not None and status >= 500:
        text = '服务器暂时不可用'
    elif status is not None:
        text = '服务器返回异常'
    elif (detail or '').startswith('网络超时'):
        text = '网络超时'
    elif (detail or '').startswith('网络错误'):
        text = '网络连接失败'
    else:
        text = '未知错误'

    if verbose and detail and detail != text:
        return f'{text}（{detail}）'
    return text


def fetch_text(url, timeout=DOWNLOAD_TIMEOUT):
    """把小型文本响应读进内存，返回 (文本或 None, 失败说明)。"""
    try:
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode('utf-8', errors='replace'), ''
    except Exception as exc:
        return None, describe_download_error(exc)


def download_file(url, output_path, show_error=True):
    """下载文件（支持中文 URL），返回 (是否成功, 失败说明)。

    失败时会清理掉写了一半的残留文件，避免半成品被后续流程当成有效文件。
    """
    output_dir = os.path.dirname(output_path) if output_path else ''
    detail = ''

    try:
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        # 移除只读属性
        remove_readonly(output_path)

        # 使用 urllib.request.urlopen 替代 urlretrieve，更好地处理编码
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})

        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            with open(output_path, 'wb') as out_file:
                # 分块下载
                chunk_size = 65536
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    out_file.write(chunk)

        return True, ''
    except Exception as exc:
        detail = describe_download_error(exc)
        # 清掉半成品，否则下次重试会从损坏状态续写。
        try:
            if output_path and os.path.exists(output_path):
                remove_readonly(output_path)
                os.remove(output_path)
        except Exception:
            pass

    if show_error:
        print_color(f'  下载失败: {friendly_download_error(detail)}', Colors.RED)
    return False, detail


class RepoPathResolver:
    """把大小写不一致的路径还原成仓库里的真实路径。

    只在下载返回 404 时按需调用，并按目录缓存结果；路径大小写正常时
    不会产生任何额外请求。依赖 Git 服务端（Gitea/Forgejo）的内容接口。
    """

    def __init__(self, file_api):
        self.available = False
        self.contents_api = ''
        self.ref = ''
        self._dir_cache = {}
        self._lock = threading.Lock()

        match = RAW_FILE_API_RE.match((file_api or '').strip())
        if match:
            parts = match.groupdict()
            self.contents_api = (
                f"{parts['origin']}/api/v1/repos/{parts['owner']}/{parts['repo']}/contents"
            )
            self.ref = parts['ref']
            self.available = True

    def _list_dir(self, repo_dir):
        """列出仓库某个目录，返回 {小写名: 真实名}。"""
        with self._lock:
            cached = self._dir_cache.get(repo_dir)
        if cached is not None:
            return cached

        mapping = {}
        if self.available:
            base = self.contents_api
            if repo_dir:
                base = f'{base}/{encode_url_path(repo_dir)}'
            url = f'{base}?ref={urllib.parse.quote(self.ref, safe="")}'
            text, _ = fetch_text(url)
            if text:
                try:
                    entries = json.loads(text)
                except ValueError:
                    entries = None
                if isinstance(entries, list):
                    for entry in entries:
                        name = entry.get('name') if isinstance(entry, dict) else None
                        if name:
                            mapping[name.lower()] = name

        with self._lock:
            self._dir_cache[repo_dir] = mapping
        return mapping

    def resolve(self, rel_path):
        """逐段还原真实大小写；任意一段匹配不到就返回 None。"""
        if not self.available or not rel_path:
            return None

        segments = rel_path.replace('\\', '/').split('/')
        resolved = []
        for segment in segments:
            real_name = self._list_dir('/'.join(resolved)).get(segment.lower())
            if not real_name:
                return None
            resolved.append(real_name)
        return '/'.join(resolved)

def find_minecraft_path(base_dir):
    """查找实际的 Minecraft 目录"""
    mc_dir = os.path.join(base_dir, MINECRAFT_DIR_NAME)
    
    if not os.path.exists(mc_dir):
        print_color(f'未找到 {MINECRAFT_DIR_NAME} 目录，将创建: {mc_dir}', Colors.YELLOW)
        return mc_dir
    
    # 检查 versions 目录
    versions_dir = os.path.join(mc_dir, 'versions')
    if os.path.exists(versions_dir):
        for version_dir in os.listdir(versions_dir):
            version_path = os.path.join(versions_dir, version_dir)
            if os.path.isdir(version_path):
                mods_path = os.path.join(version_path, 'mods')
                config_path = os.path.join(version_path, 'config')
                if os.path.exists(mods_path) or os.path.exists(config_path):
                    print_color(f'找到 Minecraft 文件于: {version_path}', Colors.GREEN)
                    return version_path
    
    print_color(f'使用目录: {mc_dir}', Colors.YELLOW)
    return mc_dir

def test_file_hash(file_path, expected_hash):
    """测试文件哈希值是否匹配"""
    if not os.path.exists(file_path):
        return False
    
    local_hash = calculate_file_hash(file_path)
    return local_hash == expected_hash

def remove_obsolete_files(old_manifest, new_manifest, mc_dir, no_overwrite_files):
    """删除过时的文件"""
    if not old_manifest or 'files' not in old_manifest:
        return 0
    
    # 创建新文件路径的集合
    new_file_paths = {f['path'] for f in new_manifest['files']}
    
    removed_count = 0
    
    for old_file in old_manifest['files']:
        file_path = old_file['path']
        
        # 如果文件在旧版本中存在但新版本中不存在
        if file_path not in new_file_paths:
            # 更新器自身绝不删除：删掉玩家下次就没有更新工具了。
            if file_path.replace('\\', '/') == UPDATER_REL_PATH:
                continue

            local_path = os.path.join(mc_dir, file_path)
            is_mod = '/mods/' in file_path.replace('\\', '/') and file_path.endswith('.jar')
            
            # 不删除 options.txt 等不覆盖的文件
            if is_no_overwrite_file(file_path, no_overwrite_files):
                continue
            
            if is_mod:
                # 对于 mod，同时检查 .jar 和 .jar.disabled
                disabled_path = local_path + '.disabled'
                
                if os.path.exists(local_path):
                    print_color(f'  删除过时文件: {file_path}', Colors.YELLOW)
                    remove_readonly(local_path)
                    os.remove(local_path)
                    removed_count += 1
                
                if os.path.exists(disabled_path):
                    print_color(f'  删除过时文件: {file_path}.disabled', Colors.YELLOW)
                    remove_readonly(disabled_path)
                    os.remove(disabled_path)
                    removed_count += 1
            else:
                if os.path.exists(local_path):
                    print_color(f'  删除过时文件: {file_path}', Colors.YELLOW)
                    remove_readonly(local_path)
                    os.remove(local_path)
                    removed_count += 1
    
    return removed_count


def remove_retired_files(new_manifest, mc_dir, keep_patterns, verbose=False):
    """删除导出器发布的「已废弃文件」清单里的路径。

    清单在服务器清单的 removedFiles 字段里累积发布，与客户端本地清单无关，
    所以不管玩家从哪个版本开始更新，跑一次就能收敛（不会因为跨版本而漏删）。
    名单只包含「包里曾经有过、现在不要了」的文件，玩家自己加的东西不在里面。
    """
    retired = new_manifest.get('removedFiles') or []
    if not isinstance(retired, list) or not retired:
        return 0

    # 护栏：清单里仍然存在的路径（大小写不敏感）绝不删。
    # Windows 不区分大小写，若把纯大小写差异误当废弃，就会「刚下好就删掉」死循环。
    live_keys = {
        str(item.get('path', '')).replace('\\', '/').casefold()
        for item in (new_manifest.get('files') or [])
        if isinstance(item, dict) and item.get('path')
    }

    removed_count = 0
    for rel_path in retired:
        if not isinstance(rel_path, str) or not rel_path:
            continue
        rel_path = rel_path.replace('\\', '/')

        if rel_path.casefold() in live_keys:
            continue

        # 更新器自身绝不删除：删掉玩家下次就没有更新工具了。
        if rel_path == UPDATER_REL_PATH:
            continue

        # 玩家的保护名单（含 noOverwriteFiles 与自己配置的 keepFiles）。
        if is_no_overwrite_file(rel_path, keep_patterns):
            continue

        local_path = os.path.join(mc_dir, *rel_path.split('/'))
        for candidate in (local_path, local_path + '.disabled'):
            if not os.path.isfile(candidate):
                continue
            if verbose:
                print_color(f'  删除已废弃文件: {os.path.relpath(candidate, mc_dir)}', Colors.GRAY)
            try:
                remove_readonly(candidate)
                os.remove(candidate)
                removed_count += 1
            except OSError:
                # 文件被占用（游戏还开着）之类，跳过即可，下次再删。
                pass

    return removed_count

def encode_url_path(path):
    """URL 编码路径，正确处理中文和特殊字符"""
    path = path.replace('\\', '/')
    
    # 对路径的每个部分进行 URL 编码
    segments = path.split('/')
    encoded_segments = []
    
    for segment in segments:
        # 使用 quote 函数，safe='' 表示除了字母数字外都编码
        encoded = urllib.parse.quote(segment, safe='')
        encoded_segments.append(encoded)
    
    return '/'.join(encoded_segments)

def download_and_verify_file(file_info, file_api, mc_dir, resolver=None):
    """下载单个文件并校验哈希，供线程池调用。

    返回 (是否成功, 失败原因, 修正后的真实路径)。
    真实路径只在「仓库里的大小写与清单不一致、已自动纠正」时才有值。
    """
    rel_path = file_info['path']
    expected_hash = file_info['hash']
    api_base = file_api.rstrip('/')
    # 本地落地路径始终用清单里的 path（磁盘上的大小写）；
    # 拼下载地址时优先用 urlPath —— 它是导出器记录的「仓库里的真实大小写」，
    # 两者不同通常意味着工作区改过大小写而 git 没记录（Windows 上很常见）。
    output_path = os.path.join(mc_dir, rel_path)

    attempt_path = file_info.get('urlPath') or rel_path
    healed_path = None
    heal_attempted = False
    reason = '下载失败'

    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        file_url = f"{api_base}/overrides/{encode_url_path(attempt_path)}"
        ok, detail = download_file(file_url, output_path, show_error=False)

        if ok:
            reason = ''
            break

        reason = detail

        if http_status_of(detail) == 404:
            # 404 是确定性失败：先试着用仓库里的真实大小写还原路径。
            # 还原不了就直接放弃，不做无谓的重试。
            if resolver is not None and not heal_attempted:
                heal_attempted = True
                real = resolver.resolve('overrides/' + rel_path)
                prefix = 'overrides/'
                if real and real.startswith(prefix) and real[len(prefix):] != rel_path:
                    healed_path = real[len(prefix):]
                    attempt_path = healed_path
                    continue
            break

        if attempt < MAX_DOWNLOAD_ATTEMPTS and is_retryable_failure(detail):
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        break

    if reason:
        return False, reason, None

    if not test_file_hash(output_path, expected_hash):
        # 不保留校验失败的文件，避免把损坏文件当作有效文件继续使用。
        try:
            remove_readonly(output_path)
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass
        return False, '哈希值不匹配', None

    return True, '', healed_path


def download_files_multithread(file_list, file_api, mc_dir, download_threads,
                               resolver=None, verbose=False):
    """使用线程池并发下载文件，返回 (失败文件, 大小写已自动校正的文件)。"""
    if not file_list:
        return [], []

    worker_count = max(1, min(int(download_threads), MAX_DOWNLOAD_THREADS, len(file_list)))
    total = len(file_list)
    completed = 0
    failed_files = []
    healed_files = []

    print_color(f'开始下载，共 {total} 个文件，使用 {worker_count} 个线程...', Colors.CYAN)
    print()

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                download_and_verify_file, file_info, file_api, mc_dir, resolver
            ): file_info
            for file_info in file_list
        }

        for future in as_completed(future_map):
            file_info = future_map[future]
            file_path = file_info['path']
            completed += 1

            try:
                success, reason, healed_path = future.result()
            except Exception as e:
                success, reason, healed_path = False, f'下载线程异常: {e}', None

            print(f'[{completed}/{total}] {file_path}')
            if success:
                if healed_path:
                    healed_files.append((file_path, healed_path))
                print_color('  成功', Colors.GREEN)
            else:
                print_color(f'  失败: {friendly_download_error(reason, verbose)}', Colors.RED)
                failed_files.append((file_info, reason))

    return failed_files, healed_files


def print_healed_details(healed_files, verbose=False):
    """大小写自愈的明细。

    默认对玩家保持安静：这属于打包方的维护细节，玩家看了只会困惑。
    打包方排查时把 updater-config.json 里的 verbose 设为 true 即可看到。
    """
    if not healed_files or not verbose:
        return

    print()
    print_color(
        f'[verbose] {len(healed_files)} 个文件的仓库路径大小写与清单不一致，已自动校正并下载成功：',
        Colors.YELLOW
    )
    for manifest_path, real_path in healed_files[:20]:
        print(f'    - 清单: {manifest_path}')
        print(f'      仓库: {real_path}')
    if len(healed_files) > 20:
        print(f'    ... 还有 {len(healed_files) - 20} 个')
    print_color(
        '  建议重新导出清单（导出器会把真实路径写进 urlPath），之后就不再需要探测。',
        Colors.GRAY
    )


def ask_retry_failed_files(failed_files, verbose=False):
    """展示失败统计并询问玩家是否重试。"""
    if not failed_files:
        return False

    print()
    print_color(f'本轮有 {len(failed_files)} 个文件未更新成功：', Colors.RED)
    for file_info, reason in failed_files[:20]:
        print(f"  - {file_info['path']}（{friendly_download_error(reason, verbose)}）")
    if len(failed_files) > 20:
        print(f'  ... 还有 {len(failed_files) - 20} 个')
    print()

    answer = input('是否重试这些失败文件？(y/N): ').strip().lower()
    return answer in ('y', 'yes')


# ---- 主函数 ----

def save_local_manifest(server_manifest, local_manifest_path):
    """把服务器清单落盘为本地更新记录，并记下本次应用的更新器版本。

    记录生成在游戏目录里（local_manifest_path 由调用方指定），内容 = 服务器清单
    + updaterVersion 字段，供下次更新做差、也供排查问题时确认玩家环境。
    """
    server_manifest['updaterVersion'] = UPDATER_VERSION
    try:
        with open(local_manifest_path, 'w', encoding='utf-8') as f:
            json.dump(server_manifest, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        print_color('警告: 本地更新记录保存失败，下次启动会重新检查全部文件。', Colors.YELLOW)
        return False


def main():
    print_color('=' * 50, Colors.CYAN)
    print_color('  Minecraft 整合包自动更新工具', Colors.CYAN)
    print_color(f'  更新器版本 {UPDATER_VERSION}', Colors.GRAY)
    print_color('=' * 50, Colors.CYAN)
    print()
    
    # 获取脚本所在目录（update）
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 获取整合包根目录（update 的上级目录）
    root_dir = os.path.dirname(script_dir)

    # 加载更新器本地持久化配置。首次运行会引导玩家设置实际游戏目录。
    updater_config = load_or_create_updater_config(script_dir)
    mc_dir = updater_config['minecraftPath']
    download_threads = updater_config.get('downloadThreads', DEFAULT_DOWNLOAD_THREADS)
    verbose = bool(updater_config.get('verbose', False))
    # 玩家自己的保护名单：写在这里的文件永远不会被清理（自己加的 mod 等）。
    raw_keep_files = updater_config.get('keepFiles')
    player_keep_files = (
        [p for p in raw_keep_files if isinstance(p, str) and p]
        if isinstance(raw_keep_files, list) else []
    )

    print_color(f'整合包目录: {mc_dir}', Colors.GREEN)
    print_color(f'下载线程数: {download_threads}', Colors.GREEN)
    print()

    # 本地更新记录（版本号 + 文件清单）自 2.4 起生成在游戏目录的 update/ 子目录里，
    # 不直接放游戏目录根下，避免玩家清理目录时误删。老版本留在整合包根目录或
    # 游戏目录根的记录会自动迁移。
    state_dir = os.path.join(mc_dir, 'update')
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        # 子目录建不起来（权限等）就退回游戏目录根，不阻断更新。
        state_dir = mc_dir
    local_manifest_path = os.path.join(state_dir, MANIFEST_FILE)
    local_key = os.path.normcase(os.path.abspath(local_manifest_path))
    legacy_candidates = []
    for candidate in (
        os.path.join(root_dir, MANIFEST_FILE),   # 2.3 及以前：整合包根目录
        os.path.join(mc_dir, MANIFEST_FILE),     # 2.4 初期：游戏目录根
    ):
        key = os.path.normcase(os.path.abspath(candidate))
        if key != local_key and key not in [
            os.path.normcase(os.path.abspath(c)) for c in legacy_candidates
        ]:
            legacy_candidates.append(candidate)
    if not os.path.exists(local_manifest_path):
        for legacy_manifest_path in legacy_candidates:
            if not os.path.exists(legacy_manifest_path):
                continue
            try:
                shutil.copy2(legacy_manifest_path, local_manifest_path)
                try:
                    os.remove(legacy_manifest_path)
                except OSError:
                    pass
                print_color('已将本地更新记录迁移到游戏目录的 update 文件夹。', Colors.GRAY)
            except OSError:
                # 迁移失败（权限等）就继续用旧位置，不阻断更新。
                local_manifest_path = legacy_manifest_path
            break

    # 检查本地 manifest
    if not os.path.exists(local_manifest_path):
        print_color('错误: 游戏目录里缺少更新信息文件，无法确定当前版本。', Colors.RED)
        print_color('请确认上面显示的整合包目录正确；全新安装请先运行整合包自带的更新程序。', Colors.YELLOW)
        if verbose:
            print_color(f'[verbose] 缺少文件: {local_manifest_path}', Colors.GRAY)
        input('按回车键退出...')
        sys.exit(1)
    
    # 读取本地 manifest
    print_color('读取本地配置文件...', Colors.YELLOW)
    try:
        with open(local_manifest_path, 'r', encoding='utf-8') as f:
            local_manifest = json.load(f)
    except Exception as e:
        print_color('错误: 本地更新信息已损坏，无法读取。', Colors.RED)
        print_color('请重新下载整合包，或联系整合包作者。', Colors.YELLOW)
        if verbose:
            print_color(f'[verbose] {type(e).__name__}: {e}', Colors.GRAY)
        input('按回车键退出...')
        sys.exit(1)
    
    pack_name = local_manifest.get('name', 'Unknown')
    pack_author = local_manifest.get('author', 'Unknown')
    local_version = local_manifest.get('version', '0.0.0')
    file_api = local_manifest.get('fileApi', '')
    
    print_color(f'整合包名称: {pack_name}', Colors.GREEN)
    print_color(f'作者: {pack_author}', Colors.GREEN)
    print_color(f'本地版本: {local_version}', Colors.GREEN)
    print()
    
    # 加载额外配置（会自动检查服务器更新）
    extra_config, no_overwrite_files = load_extra_config(root_dir, file_api)
    print_color(f'已读取 {len(no_overwrite_files)} 项保护设置', Colors.GREEN)
    print()
    
    # 下载服务器 manifest
    print_color('检查服务器更新...', Colors.YELLOW)
    server_manifest_url = f'{file_api}/server-manifest.json'
    # 临时清单同样放游戏目录的 update 子目录（隐藏文件）；顺手清掉旧位置的残留。
    temp_manifest_path = os.path.join(state_dir, '.server-manifest-temp.json')
    for stale_temp_path in {
        os.path.join(script_dir, '.server-manifest-temp.json'),  # 2.3 及以前：update 脚本目录
        os.path.join(mc_dir, '.server-manifest-temp.json'),      # 2.4 初期：游戏目录根
    }:
        if (os.path.normcase(os.path.abspath(stale_temp_path))
                != os.path.normcase(os.path.abspath(temp_manifest_path))
                and os.path.exists(stale_temp_path)):
            try:
                os.remove(stale_temp_path)
            except OSError:
                pass

    manifest_ok, manifest_detail = download_file(server_manifest_url, temp_manifest_path, show_error=False)
    if not manifest_ok:
        print_color(
            f'错误: 无法获取更新信息（{friendly_download_error(manifest_detail, verbose)}）。',
            Colors.RED
        )
        print_color('请检查网络后重试；若一直失败，请联系整合包作者。', Colors.YELLOW)
        input('按回车键退出...')
        sys.exit(1)
    
    # 读取服务器 manifest
    try:
        with open(temp_manifest_path, 'r', encoding='utf-8') as f:
            server_manifest = json.load(f)
    except Exception as e:
        print_color('错误: 服务器返回的更新信息无法读取，请联系整合包作者。', Colors.RED)
        if verbose:
            print_color(f'[verbose] {type(e).__name__}: {e}', Colors.GRAY)
        input('按回车键退出...')
        sys.exit(1)
    
    server_version = server_manifest.get('version', '0.0.0')
    print_color(f'服务器版本: {server_version}', Colors.GREEN)
    print()
    
    # 比较版本
    is_version_update = local_version != server_version
    if is_version_update:
        print_color('发现新版本！准备更新...', Colors.CYAN)
    else:
        print_color('版本一致，检查文件完整性...', Colors.YELLOW)
    
    print()
    
    # Minecraft 目录已由 updater-config.json 指定并验证。
    print_color(f'使用已配置的 Minecraft 目录: {mc_dir}', Colors.GREEN)
    print()
    
    # 处理强制删除（新增）
    force_deleted = process_force_delete(extra_config, mc_dir)
    if force_deleted > 0:
        print_color(f'已删除 {force_deleted} 个指定文件', Colors.GREEN)
        print()

    # 删除「已废弃文件」：清单由导出器累积发布在服务器清单的 removedFiles 里，
    # 与客户端本地清单无关，所以每次都跑（不是只有版本变化才清）。
    # 这样玩家从任意版本开始更新都能一次收敛，跨版本也不会漏删。
    keep_patterns = list(no_overwrite_files) + list(player_keep_files)
    retired_count = remove_retired_files(server_manifest, mc_dir, keep_patterns, verbose)
    if retired_count > 0:
        print_color(f'已清理 {retired_count} 个已废弃文件', Colors.GREEN)
        print()

    # 兼容后备：老清单没有 removedFiles 字段时，仍按本地清单做差集。
    removed_count = 0
    if is_version_update:
        removed_count = remove_obsolete_files(local_manifest, server_manifest, mc_dir, no_overwrite_files)
        if removed_count > 0:
            print_color(f'已删除 {removed_count} 个过时文件', Colors.GREEN)
            print()

    # 检查文件
    need_update = []
    total_files = len(server_manifest['files'])
    matched = 0
    missing = 0
    skipped = 0
    
    print_color(f'检查文件 (共 {total_files} 个)...', Colors.YELLOW)
    start_time = time.time()
    
    for idx, file_info in enumerate(server_manifest['files'], 1):
        if idx % 200 == 0:
            elapsed = time.time() - start_time
            speed = idx / elapsed if elapsed > 0 else 0
            remaining = int((total_files - idx) / speed) if speed > 0 else 0
            print(f'进度: {idx}/{total_files} (匹配: {matched}, 缺失: {missing}, 跳过: {skipped}) - 预计剩余: {remaining}秒')
        
        file_path = file_info['path']
        expected_hash = file_info['hash']
        local_path = os.path.join(mc_dir, file_path)
        need_download = False
        
        # 检查是否是不覆盖的文件（如 options.txt）
        if is_no_overwrite_file(file_path, no_overwrite_files):
            if os.path.exists(local_path):
                # 文件存在，跳过检查和更新
                matched += 1
                skipped += 1
                continue
            else:
                # 文件不存在，需要下载
                missing += 1
                need_download = True
        else:
            is_mod = '/mods/' in file_path.replace('\\', '/') and file_path.endswith('.jar')
            
            if is_mod:
                # 检查 .jar 和 .jar.disabled
                jar_path = local_path
                disabled_path = local_path + '.disabled'
                
                jar_exists = os.path.exists(jar_path)
                disabled_exists = os.path.exists(disabled_path)
                
                if not jar_exists and not disabled_exists:
                    missing += 1
                    need_download = True
                else:
                    hash_matched = False
                    
                    if jar_exists and test_file_hash(jar_path, expected_hash):
                        hash_matched = True
                        matched += 1
                    elif disabled_exists and test_file_hash(disabled_path, expected_hash):
                        hash_matched = True
                        matched += 1
                    
                    if not hash_matched:
                        need_download = True
                        if jar_exists:
                            remove_readonly(jar_path)
                            os.remove(jar_path)
                        if disabled_exists:
                            remove_readonly(disabled_path)
                            os.remove(disabled_path)
            else:
                if not os.path.exists(local_path):
                    missing += 1
                    need_download = True
                elif test_file_hash(local_path, expected_hash):
                    matched += 1
                else:
                    need_download = True
        
        if need_download:
            need_update.append(file_info)
    
    elapsed = time.time() - start_time
    print()
    print(f'检查完成: {total_files}/{total_files} (匹配: {matched}, 缺失: {missing}, 跳过: {skipped}) - 用时: {int(elapsed)}秒')
    print()
    
    # 下载更新
    if len(need_update) == 0:
        print_color('所有文件都是最新的！', Colors.GREEN)
        final_failed_files = []

        # 更新本地 manifest
        if is_version_update:
            if save_local_manifest(server_manifest, local_manifest_path):
                print_color(f'配置文件已更新到版本 {server_version}', Colors.GREEN)
    else:
        print_color(f'需要更新 {len(need_update)} 个文件', Colors.CYAN)
        print()

        # 404 时用于反查仓库真实大小写的解析器；fileApi 不是 raw 形式时自动禁用。
        path_resolver = RepoPathResolver(file_api)

        # 首轮多线程下载
        failed_files, healed_files = download_files_multithread(
            need_update,
            file_api,
            mc_dir,
            download_threads,
            path_resolver,
            verbose
        )

        # 下载结束后统计失败文件，并允许玩家反复重试失败项。
        while failed_files and ask_retry_failed_files(failed_files, verbose):
            retry_file_infos = [file_info for file_info, _ in failed_files]
            print()
            print_color(f'重新尝试下载 {len(retry_file_infos)} 个失败文件...', Colors.CYAN)
            failed_files, retry_healed = download_files_multithread(
                retry_file_infos,
                file_api,
                mc_dir,
                download_threads,
                path_resolver,
                verbose
            )
            healed_files.extend(retry_healed)

        final_failed_files = failed_files
        fail = len(final_failed_files)
        success = len(need_update) - fail

        print()
        print_color('=' * 50, Colors.CYAN)
        print_color('  更新完成', Colors.CYAN)
        print_color(f'  成功: {success} | 失败: {fail}', Colors.CYAN)
        if removed_count > 0:
            print_color(f'  已删除: {removed_count}', Colors.CYAN)
        print_color('=' * 50, Colors.CYAN)

        if final_failed_files:
            print()
            print_color('最终仍未更新成功的文件:', Colors.RED)
            for file_info, reason in final_failed_files[:20]:
                print(f"  - {file_info['path']}（{friendly_download_error(reason, verbose)}）")
            if len(final_failed_files) > 20:
                print(f'  ... 还有 {len(final_failed_files) - 20} 个')
            print()
            print_color('请把上面这段信息发给服主/整合包作者，或稍后重新运行本工具。', Colors.YELLOW)

        print_healed_details(healed_files, verbose)

        # 更新器自己也在清单里：如果它这次被更新了，本次运行的仍是旧版本。
        normalized_updated = {info['path'].replace('\\', '/') for info in need_update}
        normalized_failed = {info['path'].replace('\\', '/') for info, _ in final_failed_files}
        if UPDATER_REL_PATH in normalized_updated and UPDATER_REL_PATH not in normalized_failed:
            print()
            print_color(
                '更新器本身已更新完成；本次运行的仍是旧版本，下次启动会自动使用新版本。',
                Colors.CYAN
            )

        # 只有所有文件最终更新成功，才更新本地 manifest。
        if is_version_update and fail == 0:
            if save_local_manifest(server_manifest, local_manifest_path):
                print()
                print_color(f'配置文件已更新到版本 {server_version}', Colors.GREEN)

    # 清理临时文件
    try:
        if os.path.exists(temp_manifest_path):
            os.remove(temp_manifest_path)
    except Exception:
        pass
    
    print()
    input('按回车键退出...')

if __name__ == '__main__':
    main()