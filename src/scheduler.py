"""
主调度模块
串联 Phase0(解析) → Phase1(工作流1) → Phase2(工作流2a-2e) → Phase3(下载)
"""
import asyncio
import aiohttp
import json
import os
from datetime import datetime
from pathlib import Path

from src.excel_parser import parse_excel
from src.coze_client import CozeClient
from src.ledger import (
    create_ledger, batch_update, finalize_ledger, read_retry_ids,
    STATUS_PENDING, STATUS_PROCESSING, STATUS_SUCCESS, STATUS_FAILED
)
from src.downloader import batch_download, build_download_tasks

PHASE2_CONCURRENCY = int(os.getenv('PHASE2_CONCURRENCY', '3'))


def _make_entry_id(abbr: str, idx: int, date_str: str) -> str:
    """生成台账条目 ID，如 CXW_001_20250515"""
    date_clean = str(date_str).replace('.', '').replace('/', '').strip()
    # 补全年份（5.15 → 20250515）
    if len(date_clean) == 3 or len(date_clean) == 4:
        date_clean = f'2025{date_clean.zfill(4)}'
    return f'{abbr}_{idx:03d}_{date_clean}'


def _parse_group_to_entries(group_name: str, data: list[str], abbr: str) -> list[dict]:
    """
    将单组的 JSON 字符串数组拆解成台账条目列表
    数据格式：[元数据..., 列标题行, 数据行1, 数据行2, ...]
    """
    # 找列标题行（含"日期"字样的那一行）
    header_idx = next((i for i, s in enumerate(data) if '日期' in s and '人物' in s), -1)
    if header_idx == -1:
        return []

    headers = [h.strip() for h in data[header_idx].split(' // ')]

    entries = []
    data_rows = data[header_idx + 1:]

    for seq, row_str in enumerate(data_rows, start=1):
        values = [v.strip() for v in row_str.split(' // ')]
        row_dict = dict(zip(headers, values))

        date_str = row_dict.get('日期', '')
        entry_id = _make_entry_id(abbr, seq, date_str)

        entries.append({
            'id':       entry_id,
            '组名':     group_name,
            '日期':     date_str,
            '人物a':    row_dict.get('人物a', row_dict.get('人物A', '/')),
            '人物b':    row_dict.get('人物b', row_dict.get('人物B', '/')),
            '场景':     row_dict.get('场景', '/'),
            '道具':     row_dict.get('道具', '/'),
            '台词':     row_dict.get('视频中人物说的台词', row_dict.get('台词', '/')),
            '字幕台词': row_dict.get('最终字幕的台词', row_dict.get('最终字幕', '/')),
            '当前阶段': 'Phase0',
            '状态':     STATUS_PENDING,
            '本地文件名': f'{entry_id}.mp4',
        })

    return entries


async def _run_phase1(
    groups: dict[str, list[str]],
    config: dict,
    token1: str,
    ledger_path: str
) -> dict[str, list[str]]:
    """Phase1：并发调用所有组的工作流1"""
    workflow_id = config['workflows']['workflow1']
    timeout = aiohttp.ClientTimeout(total=1800)  # 30分钟

    print(f"\n🚀 Phase1 开始：并发调用 {len(groups)} 个组的工作流1\n")

    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = CozeClient(token=token1, session=session)

        async def call_one(group_name: str, data: list[str]):
            result = await client.run_workflow(workflow_id, data, group_name)
            return group_name, result

        tasks = [call_one(gn, d) for gn, d in groups.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    phase1_results = {}
    ledger_updates = []

    for item in results:
        if isinstance(item, Exception):
            print(f"  ❌ Phase1 异常：{item}")
            continue
        group_name, result = item
        if result is None:
            print(f"  ❌ [{group_name}] 工作流1 返回空")
            continue
        phase1_results[group_name] = result
        # TODO: 解析 result 中每条数据的图片URL、提示词，映射到 ledger_updates

    if ledger_updates:
        batch_update(ledger_path, ledger_updates)

    print(f"\n✅ Phase1 完成：{len(phase1_results)}/{len(groups)} 组成功\n")
    return phase1_results


async def _run_phase2_group(
    group_name: str,
    input_data: list[str],
    config: dict,
    token2: str,
    ledger_path: str,
    semaphore: asyncio.Semaphore
) -> list[str]:
    """Phase2：单组串联跑 2a→2b→2c→2d→2e"""
    async with semaphore:
        workflow_ids = config['workflows']
        timeout = aiohttp.ClientTimeout(total=3600)  # 每个工作流最长1小时

        async with aiohttp.ClientSession(timeout=timeout) as session:
            client = CozeClient(token=token2, session=session)
            current_data = input_data
            stages = ['workflow2a', 'workflow2b', 'workflow2c', 'workflow2d', 'workflow2e']
            stage_names = ['2a生成视频', '2b清字幕', '2c修配音', '2d超分', '2e加字幕']

            for wf_key, stage_name in zip(stages, stage_names):
                wf_id = workflow_ids.get(wf_key, '')
                if not wf_id or wf_id.startswith('待填入'):
                    print(f"  ⚠️  [{group_name}][{stage_name}] 工作流ID未配置，跳过")
                    continue

                print(f"  ▶️  [{group_name}] 开始 {stage_name}")
                result = await client.run_workflow(wf_id, current_data, f'{group_name}/{stage_name}')

                if result is None:
                    print(f"  ❌  [{group_name}][{stage_name}] 失败，终止此组")
                    return []

                # 提取下一阶段的输入（工作流返回的 arraystring）
                current_data = result.get('output', result.get('data', current_data))
                if isinstance(current_data, str):
                    try:
                        current_data = json.loads(current_data)
                    except Exception:
                        current_data = [current_data]

            return current_data


async def run_all(
    excel_path: str,
    config: dict,
    token1: str,
    token2: str,
    ledger_path: str
):
    """完整流程入口"""
    # ── Phase0：解析 Excel ──────────────────────────────────────────────────
    print("\n═══════════════════════════════════")
    print("  Phase 0：解析 Excel")
    print("═══════════════════════════════════")

    groups = parse_excel(excel_path, config)
    group_abbr = config.get('group_abbr', {})

    all_entries = []
    for group_name, data in groups.items():
        abbr = group_abbr.get(group_name, group_name[:3].upper())
        entries = _parse_group_to_entries(group_name, data, abbr)
        all_entries.extend(entries)

    create_ledger(ledger_path, all_entries)
    print(f"  共 {len(all_entries)} 条数据初始化完成")

    # ── Phase1：并发调用工作流1 ───────────────────────────────────────────────
    print("\n═══════════════════════════════════")
    print("  Phase 1：图片生成（工作流1）")
    print("═══════════════════════════════════")

    phase1_results = await _run_phase1(groups, config, token1, ledger_path)

    # ── Phase2：分组串联工作流2a-2e ──────────────────────────────────────────
    print("\n═══════════════════════════════════")
    print(f"  Phase 2：视频生成（工作流2a-2e，并发{PHASE2_CONCURRENCY}组）")
    print("═══════════════════════════════════")

    semaphore = asyncio.Semaphore(PHASE2_CONCURRENCY)
    p2_tasks = []
    for group_name, result_data in phase1_results.items():
        # 将工作流1的输出整理成 2a 的输入
        input_for_2a = result_data.get('output', [])
        if isinstance(input_for_2a, str):
            try:
                input_for_2a = json.loads(input_for_2a)
            except Exception:
                input_for_2a = [input_for_2a]

        p2_tasks.append(_run_phase2_group(
            group_name, input_for_2a, config, token2, ledger_path, semaphore
        ))

    await asyncio.gather(*p2_tasks, return_exceptions=True)

    # ── Phase3：整理台账 + 下载 ──────────────────────────────────────────────
    print("\n═══════════════════════════════════")
    print("  Phase 3：下载视频 + 整理台账")
    print("═══════════════════════════════════")

    finalize_ledger(ledger_path)
    download_tasks = build_download_tasks(all_entries, group_abbr)
    await batch_download(download_tasks)

    print("\n🎉 全部流程完成！")
    print(f"   台账路径：{ledger_path}")
    print(f"   视频目录：data/output/videos/\n")
