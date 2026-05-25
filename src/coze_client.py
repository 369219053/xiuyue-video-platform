"""
Coze API 调用模块
支持异步并发、失败自动重试（指数退避）
"""
import asyncio
import aiohttp
import json
import os
import time
from typing import Optional


COZE_BASE_URL = os.getenv('COZE_BASE_URL', 'https://api.coze.cn')
MAX_RETRIES = 3          # 最大重试次数
RETRY_BASE_DELAY = 5     # 首次重试等待秒数（指数退避基数）


class CozeClient:
    def __init__(self, token: str, session: Optional[aiohttp.ClientSession] = None):
        self.token = token
        self.session = session  # 外部传入 session（复用连接）

    def _headers(self) -> dict:
        return {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
        }

    async def run_workflow(
        self,
        workflow_id: str,
        input_data: list[str],
        group_name: str = '',
        retry: int = 0
    ) -> Optional[dict]:
        """
        调用 Coze 工作流
        :param workflow_id: 工作流 ID
        :param input_data:  传入的字符串数组（arraystring）
        :param group_name:  组名（用于日志）
        :param retry:       当前重试次数（内部使用）
        :return: 工作流返回的 data 字段，失败返回 None
        """
        url = f'{COZE_BASE_URL}/v1/workflow/run'
        payload = {
            'workflow_id': workflow_id,
            'parameters': {
                'input': input_data
            }
        }

        tag = f'[{group_name}][工作流{workflow_id[-6:]}]'

        try:
            async with self.session.post(url, headers=self._headers(), json=payload) as resp:
                if resp.status == 429:
                    # 限流：等待后重试
                    wait = RETRY_BASE_DELAY * (2 ** retry)
                    print(f"  ⚠️  {tag} 限流(429)，{wait}s 后重试（第{retry+1}次）")
                    await asyncio.sleep(wait)
                    if retry < MAX_RETRIES:
                        return await self.run_workflow(workflow_id, input_data, group_name, retry + 1)
                    return None

                if resp.status != 200:
                    body = await resp.text()
                    print(f"  ❌  {tag} HTTP 错误 {resp.status}：{body[:200]}")
                    if retry < MAX_RETRIES:
                        await asyncio.sleep(RETRY_BASE_DELAY * (2 ** retry))
                        return await self.run_workflow(workflow_id, input_data, group_name, retry + 1)
                    return None

                result = await resp.json()

                # Coze 返回格式：{"code": 0, "msg": "success", "data": "..."}
                if result.get('code') != 0:
                    print(f"  ❌  {tag} 业务错误：{result.get('msg', '未知错误')}")
                    if retry < MAX_RETRIES:
                        await asyncio.sleep(RETRY_BASE_DELAY * (2 ** retry))
                        return await self.run_workflow(workflow_id, input_data, group_name, retry + 1)
                    return None

                # data 字段可能是 JSON 字符串，需要再次解析
                raw_data = result.get('data', '{}')
                if isinstance(raw_data, str):
                    try:
                        data = json.loads(raw_data)
                    except json.JSONDecodeError:
                        data = {'raw': raw_data}
                else:
                    data = raw_data

                print(f"  ✅  {tag} 调用成功")
                return data

        except asyncio.TimeoutError:
            print(f"  ⚠️  {tag} 请求超时，重试（第{retry+1}次）")
            if retry < MAX_RETRIES:
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** retry))
                return await self.run_workflow(workflow_id, input_data, group_name, retry + 1)
            return None

        except aiohttp.ClientError as e:
            print(f"  ❌  {tag} 网络错误：{e}")
            if retry < MAX_RETRIES:
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** retry))
                return await self.run_workflow(workflow_id, input_data, group_name, retry + 1)
            return None


async def run_workflow_with_session(
    token: str,
    workflow_id: str,
    input_data: list[str],
    group_name: str = '',
    timeout_seconds: int = 1200  # 默认超时20分钟
) -> Optional[dict]:
    """
    创建独立 session 调用工作流（单次调用场景）
    """
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = CozeClient(token=token, session=session)
        return await client.run_workflow(workflow_id, input_data, group_name)
