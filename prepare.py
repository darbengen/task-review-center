#!/usr/bin/env python3
"""Prepare portable MCP paths; does not change any host configuration."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'scripts'))
from publish_runtime import publish

def main():
    if sys.platform != 'darwin':
        raise SystemExit('本分享版仅支持 macOS。')
    version=json.loads((ROOT/'.codex-plugin/plugin.json').read_text())['version']
    config={'mcpServers':{'task-review-center':{'command':sys.executable,'args':[str(ROOT/'scripts/launcher.py'),'--generation',version]}}}
    (ROOT/'.mcp.json').write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n')
    publish(ROOT)
    print('准备完成。插件目录：'+str(ROOT))
    print('请按 README.md 将插件登记到 Codex，然后打开新对话。')
if __name__=='__main__':main()
