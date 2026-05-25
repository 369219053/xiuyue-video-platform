"""
视频批量下载模块
从台账中读取视频 URL，按命名规则异步下载到本地
"""
import asyncio
import aiohttp
import aiofiles
import os
from pathlib import Path


DOWNLOAD_CONCURRENCY = int(os.getenv('DOWNLOAD_CONCURRENCY', '5'))
DOWNLOAD_TIMEOUT = 300  # 单个视频最长等待 5 分钟


async def _download_one(
    session: aiohttp.ClientSession,
    url: str,
    save_path: Path,
    semaphore: asyncio.Semaphore
) -> bool:
    """下载单个视频，返回是否成功"""
    async with semaphore:
        try:
            timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
            async with session.get(url, timeout=timeout) as resp:
                if resp.status != 200:
                    print(f"  ❌ 下载失败（HTTP {resp.status}）：{url[:80]}")
                    return False

                save_path.parent.mkdir(parents=True, exist_ok=True)
                async with aiofiles.open(save_path, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        await f.write(chunk)

            print(f"  ✅ 已下载：{save_path.name}")
            return True

        except asyncio.TimeoutError:
            print(f"  ⚠️ 下载超时：{save_path.name}")
            return False
        except Exception as e:
            print(f"  ❌ 下载异常：{save_path.name} → {e}")
            return False


async def batch_download(tasks: list[dict], output_base: str = 'data/output/videos') -> dict[str, bool]:
    """
    批量下载视频
    tasks 格式：
    [
      {'id': 'CXW_001', '组名': '曹心唯组', 'url': 'https://...', 'filename': 'CXW_001_20250515.mp4'},
      ...
    ]
    Returns: {'CXW_001': True/False, ...}
    """
    semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    results = {}

    print(f"\n📥 开始下载，共 {len(tasks)} 个视频（并发 {DOWNLOAD_CONCURRENCY}）\n")

    async with aiohttp.ClientSession() as session:
        coros = []
        for task in tasks:
            entry_id  = task['id']
            group     = task['组名']
            url       = task['url']
            filename  = task['filename']

            # 按组分子文件夹存放
            save_path = Path(output_base) / group / filename

            async def _job(eid=entry_id, u=url, sp=save_path):
                ok = await _download_one(session, u, sp, semaphore)
                results[eid] = ok

            coros.append(_job())

        await asyncio.gather(*coros)

    success = sum(1 for v in results.values() if v)
    print(f"\n📥 下载完成：{success}/{len(tasks)} 成功\n")
    return results


def build_download_tasks(ledger_entries: list[dict], group_abbr: dict) -> list[dict]:
    """
    从台账条目列表中提取待下载任务
    只下载状态=✅成功 且 最终视频URL 非空的条目
    """
    tasks = []
    for entry in ledger_entries:
        if entry.get('状态') != '✅成功':
            continue
        url = entry.get('最终视频URL', '')
        if not url or url == '/':
            continue

        filename = entry.get('本地文件名', '')
        if not filename:
            continue

        tasks.append({
            'id':      entry['id'],
            '组名':    entry['组名'],
            'url':     url,
            'filename': filename,
        })

    return tasks
