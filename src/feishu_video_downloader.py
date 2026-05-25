"""
飞书多维表格视频下载器（直链版）
- 直接用已知的阿里云OSS直链下载，无需飞书鉴权
- 命名格式：副表名称_行号.mp4
用法：python3 src/feishu_video_downloader.py
"""
import json
import re
import requests
from pathlib import Path

# ============================================================
# 配置
# ============================================================
OUTPUT_DIR = Path("data/output/下载视频")

# ============================================================
# 已提取的视频数据（从飞书 clientvars 接口获取）
# 格式：{ "副表名称": [(rank, url), ...] }
# ============================================================
TABLE_DATA = {
    "5.15_刘原原组_娇茵舒凝胶": [
        ("i000i3ri8", "https://qingyun-subtitle.oss-cn-beijing.aliyuncs.com/videos/1779164109826_out.mp4"),
        ("i000iq8lc", "https://qingyun-subtitle.oss-cn-beijing.aliyuncs.com/videos/1779163863833_out.mp4"),
        ("i000jcpog", "https://qingyun-subtitle.oss-cn-beijing.aliyuncs.com/videos/1779164841081_out.mp4"),
        ("i000jz6rk", "https://qingyun-subtitle.oss-cn-beijing.aliyuncs.com/videos/1779164034967_out.mp4"),
        ("i000klnuo", "https://qingyun-subtitle.oss-cn-beijing.aliyuncs.com/videos/1779164109883_out.mp4"),
        ("i000l84xs", "https://qingyun-subtitle.oss-cn-beijing.aliyuncs.com/videos/1779165035506_out.mp4"),
        ("i000lum0w", "https://qingyun-subtitle.oss-cn-beijing.aliyuncs.com/videos/1779164082068_out.mp4"),
        ("i000mh340", "https://qingyun-subtitle.oss-cn-beijing.aliyuncs.com/videos/1779164173937_out.mp4"),
        ("i000n3k74", "https://qingyun-subtitle.oss-cn-beijing.aliyuncs.com/videos/1779163955122_out.mp4"),
        ("i000nq1a8", "https://qingyun-subtitle.oss-cn-beijing.aliyuncs.com/videos/1779164771865_out.mp4"),
    ]
}


def download_video(url: str, save_path: Path):
    """下载视频文件（支持断点续传判断）"""
    print(f"  ⬇ 下载 {save_path.name} ...")
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    save_path.parent.mkdir(parents=True, exist_ok=True)
    with open(save_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
    size_mb = save_path.stat().st_size / 1024 / 1024
    print(f"  ✅ 已保存 {save_path.name} ({size_mb:.1f} MB)")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    total_downloaded = 0
    download_log = []

    for table_name, records in TABLE_DATA.items():
        # 按 rank 排序（字典序即飞书视觉行序）
        sorted_records = sorted(records, key=lambda r: r[0])
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', table_name)
        table_dir = OUTPUT_DIR / safe_name
        table_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n📂 副表: {table_name}  共 {len(sorted_records)} 条")

        for i, (rank, url) in enumerate(sorted_records, 1):
            filename = f"{safe_name}_{i}.mp4"
            save_path = table_dir / filename

            if save_path.exists():
                print(f"  ⏭ 已存在: {filename}")
                continue

            try:
                download_video(url, save_path)
                download_log.append({"table": table_name, "row": i, "file": str(save_path), "url": url})
                total_downloaded += 1
            except Exception as e:
                print(f"  ❌ 下载失败: {filename} -> {e}")

    # 保存下载日志
    log_path = OUTPUT_DIR / "download_log.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(download_log, f, ensure_ascii=False, indent=2)

    print(f"\n🎉 全部完成！共下载 {total_downloaded} 个视频")
    print(f"   保存位置: {OUTPUT_DIR.resolve()}")
    print(f"   下载日志: {log_path}")


if __name__ == "__main__":
    main()
