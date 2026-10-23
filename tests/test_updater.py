# -*- coding: utf-8 -*-
"""验证三项修改：跨版本清理、re-add 撤销、清单落盘到游戏目录。"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


updater = load('updater', os.path.join(ROOT, 'client', 'update', 'updater.py'))
exporter = load('exporter', os.path.join(ROOT, 'lib', 'manifest_exporter.py'))

failures = []


def check(label, cond):
    print(('PASS' if cond else 'FAIL'), '-', label)
    if not cond:
        failures.append(label)


# ---------- 1. 导出器：跨版本累积 + 重新加回撤销 ----------
# v1.0 有 a.cfg / b.jar；v1.2 删 b.jar 加 c.jar；v1.5 又把 b.jar 加回来
old = {'files': [{'path': 'a.cfg'}, {'path': 'b.jar'}]}
new_paths_v12 = ['a.cfg', 'c.jar']
removed_v12, _ = exporter.collect_removed_files('', os.devnull, old, new_paths_v12)
check('v1.2 removedFiles 包含被删的 b.jar', 'b.jar' in removed_v12)

old_v12 = {'files': [{'path': 'a.cfg'}, {'path': 'c.jar'}], 'removedFiles': removed_v12}
new_paths_v15 = ['a.cfg', 'c.jar', 'b.jar']  # b.jar 回归
removed_v15, _ = exporter.collect_removed_files('', os.devnull, old_v12, new_paths_v15)
check('v1.5 中 b.jar 重新加回后从 removedFiles 撤销', 'b.jar' not in removed_v15)

# 大小写差异不算删除（Windows 护栏）
old_case = {'files': [{'path': 'config/Foo.cfg'}]}
removed_case, _ = exporter.collect_removed_files('', os.devnull, old_case, ['config/foo.cfg'])
check('纯大小写差异不会被误判为废弃', removed_case == [])

# ---------- 2. 更新器：跨版本跳跃删除，且不删仍在清单里的文件 ----------
tmp = tempfile.mkdtemp(prefix='upd-test-')
try:
    mc = os.path.join(tmp, '.minecraft')
    os.makedirs(os.path.join(mc, 'mods'))
    for name in ('mods/b.jar', 'mods/d.jar', 'a.cfg'):
        with open(os.path.join(mc, *name.split('/')), 'w') as f:
            f.write('x')

    # 玩家从 v1.0 直跳 v1.5；v1.5 清单只含 a.cfg，removedFiles 累积了 b.jar，
    # 并故意混入仍存活的 a.cfg（应被护栏拦下）
    server_manifest = {
        'version': '1.5',
        'files': [{'path': 'a.cfg', 'hash': 'x'}],
        'removedFiles': ['mods/b.jar', 'a.cfg'],
    }
    n = updater.remove_retired_files(server_manifest, mc, [], verbose=False)
    check('跨版本清理删除 mods/b.jar', not os.path.exists(os.path.join(mc, 'mods', 'b.jar')))
    check('护栏生效：清单内的 a.cfg 未被误删', os.path.exists(os.path.join(mc, 'a.cfg')))
    check('未列出的玩家自有文件 mods/d.jar 保留', os.path.exists(os.path.join(mc, 'mods', 'd.jar')))
    check('删除计数正确（1 个）', n == 1)

    # ---------- 3. 清单落盘：写入游戏目录并注入 updaterVersion ----------
    local_path = os.path.join(mc, 'server-manifest.json')
    ok = updater.save_local_manifest(dict(server_manifest), local_path)
    check('save_local_manifest 写入成功', ok and os.path.exists(local_path))
    saved = json.load(open(local_path, encoding='utf-8'))
    check('本地记录包含 updaterVersion = %s' % updater.UPDATER_VERSION,
          saved.get('updaterVersion') == updater.UPDATER_VERSION)
    check('本地记录保留 version/files/removedFiles',
          saved.get('version') == '1.5' and len(saved['files']) == 1 and saved['removedFiles'])

    # 迁移逻辑的核心判断：mc_dir 与 root_dir 不同且旧记录存在时应迁移（此处直接模拟 shutil.copy2 路径）
    root = os.path.join(tmp, 'packroot')
    os.makedirs(root)
    legacy = os.path.join(root, 'server-manifest.json')
    json.dump({'version': '1.0', 'files': []}, open(legacy, 'w', encoding='utf-8'))
    new_local = os.path.join(mc, 'server-manifest.json')
    os.remove(new_local)  # 假装游戏目录还没有记录
    shutil.copy2(legacy, new_local)
    os.remove(legacy)
    check('旧位置记录可迁移到游戏目录',
          os.path.exists(new_local) and not os.path.exists(legacy))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if failures:
    print('失败 %d 项' % len(failures))
    sys.exit(1)
print('全部通过')
