"""
工作流1调用脚本

功能：
  - 读取指定文件夹下的所有 JSON 文件（每个文件对应一个组）
  - 逐组调用 Coze 工作流1（图片生成）
  - 每组结果实时追加到 data/工作流1返回结果/各组ai表格资料对接.md
  - 失败的组记录到 data/工作流1返回结果/失败记录.txt，不影响其他组

用法（直接运行）：
  python src/workflow1_runner.py
  python src/workflow1_runner.py --folder data/input/各组ai表格资料对接/
  python src/workflow1_runner.py --groups 王威威组 曹心唯组
  python src/workflow1_runner.py --clear   # 清空结果文件后重新跑
"""

import asyncio
import aiohttp
import json
import os
import sys
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

# ── 路径常量 ──────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
ENV_PATH = ROOT / '.env'
DEFAULT_INPUT_FOLDER = ROOT / 'data' / 'input' / 'parsed'
RESULT_DIR = ROOT / 'data' / '工作流1返回结果'
RESULT_FILE = RESULT_DIR / '各组ai表格资料对接.md'
FAIL_LOG = RESULT_DIR / '失败记录.txt'
PROGRESS_LOG = RESULT_DIR / '进度.log'

# ── API 常量 ──────────────────────────────────────────────
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5
WORKFLOW_TIMEOUT = 3600  # 60分钟超时

load_dotenv(ENV_PATH)
BASE_URL = os.getenv('COZE_BASE_URL', 'https://api.coze.cn')

# 账号配置：支持多账号并行
ACCOUNTS = [
    {
        'name': '账号1',
        'token': os.getenv('COZE_TOKEN_1', ''),
        'workflow_id': os.getenv('WORKFLOW1_ID', ''),
    },
    {
        'name': '账号2',
        'token': os.getenv('COZE_TOKEN_2', ''),
        'workflow_id': os.getenv('WORKFLOW1_ID_2', ''),
    },
]


def _validate_env():
    """检查必要的环境变量"""
    for acc in ACCOUNTS:
        missing = []
        if not acc['token']:
            missing.append(f"{acc['name']} token")
        if not acc['workflow_id']:
            missing.append(f"{acc['name']} workflow_id")
        if missing:
            print(f'❌ .env 缺少配置：{", ".join(missing)}')
            sys.exit(1)


def _load_group_files(folder: Path, only_groups: list[str] = None) -> list[tuple[str, list]]:
    """
    加载文件夹内所有 JSON 文件
    返回：[(组名, 数据列表), ...]
    """
    if not folder.exists():
        print(f'❌ 文件夹不存在：{folder}')
        sys.exit(1)

    files = sorted(folder.glob('*.json'))
    if not files:
        print(f'❌ 文件夹内没有 JSON 文件：{folder}')
        sys.exit(1)

    groups = []
    for f in files:
        name = f.stem  # 去掉 .json 后缀作为组名
        if only_groups and name not in only_groups:
            continue
        with open(f, encoding='utf-8') as fp:
            data = json.load(fp)
        groups.append((name, data))
        print(f'  📂 已加载：{name}（{len(data)} 条）')

    return groups


def _append_result(group_name: str, result: dict):
    """将单组结果追加到结果文件"""
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    # 追加模式：文件已有内容时先加换行分隔
    prefix = '\n' if RESULT_FILE.exists() and RESULT_FILE.stat().st_size > 0 else ''
    with open(RESULT_FILE, 'a', encoding='utf-8') as f:
        f.write(prefix)
        json.dump(result, f, ensure_ascii=False, indent=4)
    print(f'  💾 已保存：{group_name} → {RESULT_FILE.relative_to(ROOT)}')


def _append_fail_log(group_name: str, reason: str):
    """记录失败信息"""
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(FAIL_LOG, 'a', encoding='utf-8') as f:
        f.write(f'[{ts}] {group_name}: {reason}\n')


def _log(msg: str):
    """同时输出到终端和进度日志文件（实时刷新）"""
    ts = datetime.now().strftime('%H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_LOG, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


async def _call_workflow(session: aiohttp.ClientSession, group_name: str,
                          data: list, account: dict, retry: int = 0) -> Optional[dict]:
    """调用工作流1，支持自动重试，指定账号"""
    url = f'{BASE_URL}/v1/workflow/run'
    headers = {'Authorization': f'Bearer {account["token"]}', 'Content-Type': 'application/json'}
    payload = {'workflow_id': account['workflow_id'], 'parameters': {'input': data}}

    try:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status == 429:
                wait = RETRY_BASE_DELAY * (2 ** retry)
                _log(f'  ⚠️  [{group_name}] 限流(429)，{wait}s 后重试（第{retry+1}次）')
                await asyncio.sleep(wait)
                if retry < MAX_RETRIES:
                    return await _call_workflow(session, group_name, data, account, retry + 1)
                return None

            if resp.status != 200:
                body = await resp.text()
                _log(f'  ❌  [{group_name}] HTTP {resp.status}：{body[:200]}')
                if retry < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BASE_DELAY * (2 ** retry))
                    return await _call_workflow(session, group_name, data, account, retry + 1)
                return None

            raw = await resp.json()
            if raw.get('code') != 0:
                msg = raw.get('msg', '未知错误')
                _log(f'  ❌  [{group_name}] 业务错误：{msg}')
                if retry < MAX_RETRIES:
                    await asyncio.sleep(RETRY_BASE_DELAY * (2 ** retry))
                    return await _call_workflow(session, group_name, data, account, retry + 1)
                return None

            # data 字段是 JSON 字符串，需要再解析一次
            inner = raw.get('data', '{}')
            if isinstance(inner, str):
                return json.loads(inner)
            return inner

    except asyncio.TimeoutError:
        _log(f'  ⚠️  [{group_name}] 超时，重试（第{retry+1}次）')
        if retry < MAX_RETRIES:
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** retry))
            return await _call_workflow(session, group_name, data, account, retry + 1)
        return None

    except aiohttp.ClientError as e:
        _log(f'  ❌  [{group_name}] 网络错误：{e}')
        if retry < MAX_RETRIES:
            await asyncio.sleep(RETRY_BASE_DELAY * (2 ** retry))
            return await _call_workflow(session, group_name, data, account, retry + 1)
        return None


async def _run_single_group(session: aiohttp.ClientSession, account: dict,
                             name: str, data: list, results: dict, failed: list):
    """单组调用，供并发使用"""
    acc_name = account['name']
    _log(f'⏳ [{acc_name}] 开始处理：{name}（{len(data)} 条）')
    result = await _call_workflow(session, name, data, account)
    if result is None:
        _log(f'❌ [{acc_name}] {name} 失败，已跳过')
        _append_fail_log(name, f'调用失败，已重试{MAX_RETRIES}次')
        failed.append(name)
    else:
        _append_result(name, result)
        img_count = len(result.get('image_url_list', []))
        _log(f'✅ [{acc_name}] {name} 完成，返回 {img_count} 张图片')
        results[name] = result


async def _run_account_batch(session: aiohttp.ClientSession, account: dict,
                              groups: list, results: dict, failed: list):
    """单个账号处理分配到的组（全部并发同时跑）"""
    await asyncio.gather(*[
        _run_single_group(session, account, name, data, results, failed)
        for name, data in groups
    ])


async def run_all(folder: Path, only_groups: list[str] = None, clear: bool = False):
    """主入口：双账号均分并行调用工作流1"""
    _validate_env()

    # 清空文件
    if clear:
        for f in [RESULT_FILE, PROGRESS_LOG]:
            if f.exists():
                f.unlink()
        _log('🗑️  已清空结果文件和进度日志')

    groups = _load_group_files(folder, only_groups)
    if not groups:
        _log('❌ 没有找到需要处理的组')
        return

    total = len(groups)

    # 均分：交替分配到两个账号（按排序后的文件名交替）
    batch1 = groups[0::2]   # 索引 0,2,4,6,8 → 账号1
    batch2 = groups[1::2]   # 索引 1,3,5,7,9 → 账号2

    _log(f'🚀 双账号并行启动，共 {total} 个组')
    _log(f'   账号1：{[g[0] for g in batch1]}')
    _log(f'   账号2：{[g[0] for g in batch2]}')
    _log(f'📋 实时监控：tail -f {PROGRESS_LOG.relative_to(ROOT)}')

    results = {}
    failed = []

    timeout = aiohttp.ClientTimeout(total=WORKFLOW_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await asyncio.gather(
            _run_account_batch(session, ACCOUNTS[0], batch1, results, failed),
            _run_account_batch(session, ACCOUNTS[1], batch2, results, failed),
        )

    # ── 汇总 ──────────────────────────────────────────────
    success = total - len(failed)
    _log('─' * 40)
    _log(f'🎉 全部完成：{success}/{total} 个组成功')
    if failed:
        _log(f'❌ 失败：{failed}，详情见 {FAIL_LOG.name}')
    _log(f'📄 结果文件：{RESULT_FILE.relative_to(ROOT)}')


def main():
    parser = argparse.ArgumentParser(description='调用 Coze 工作流1并保存结果')
    parser.add_argument('--folder', type=Path, default=DEFAULT_INPUT_FOLDER,
                        help='JSON 文件所在文件夹（默认：data/input/parsed/）')
    parser.add_argument('--groups', nargs='+', default=None,
                        help='只处理指定的组（空格分隔），默认处理全部')
    parser.add_argument('--clear', action='store_true',
                        help='清空结果文件后重新写入（默认是追加）')
    args = parser.parse_args()

    asyncio.run(run_all(args.folder, args.groups, args.clear))


if __name__ == '__main__':
    main()
