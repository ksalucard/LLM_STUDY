# LLM_STUDY

## Ubuntu 24.04：OpenClaw 双体系配置（Kimi 单体 + Qwen 多代理）

你现在的状态是：**OpenClaw 已安装，但还没配置**。这份文档专门解决这个问题，并且严格区分：

- **【在 Bash 执行】**：终端里跑命令
- **【打开文件编辑】**：改哪个文件（给出完整路径）

目标：

1. **Kimi 单体代理**（只用 Kimi，不参与路由）
2. **Qwen Multi-Agent**（主模型分派给不同子代理）

---

## 0) 先确认：OpenClaw 的配置文件到底在哪

不同版本 OpenClaw 的配置路径可能不同。先自动探测，避免你改错文件。

### 0.1 检查安装与版本

**【在 Bash 执行】**

```bash
which openclaw
openclaw --version
```

### 0.2 查看帮助，找 config 参数

**【在 Bash 执行】**

```bash
openclaw --help | sed -n '1,160p'
```

> 重点看有没有 `--config`、`config path`、`profiles` 之类参数。  
> 如果支持 `--config`，建议你统一使用：`~/ai-stack/openclaw/config.yaml`。

---


## 0.5) 防重装：先做“无损自检 + 备份”

如果你不想反复重装系统，这一步强烈建议先做。核心原则：

- 先检查，不覆盖。
- 先备份，再修改。
- 每一步都可回滚。

### 0.5.1 检查端口占用（避免服务互相冲突）

**【在 Bash 执行】**

```bash
ss -lntp | rg ':4000|:8000|:8080' || true
```

### 0.5.2 备份你现有 OpenClaw 配置（如果有）

**【在 Bash 执行】**

```bash
mkdir -p ~/ai-stack/backup
# 如果你已有配置文件，把路径替换成你自己的实际路径
cp -av ~/ai-stack/openclaw/config.yaml ~/ai-stack/backup/config.yaml.bak.$(date +%F-%H%M%S) 2>/dev/null || true
```

### 0.5.3 先做“最小可逆”验证，不直接改系统级服务

**【在 Bash 执行】**

```bash
# 用当前 shell 临时环境变量测试，不写入 ~/.bashrc
export KIMI_API_KEY="你的Kimi Key"
export QWEN_API_KEY="你的Qwen Key"
```

> 只有你确认链路完全跑通后，再考虑写入 `~/.bashrc` 或 systemd。

---
## 1) 安装基础依赖

### 1.1 系统包

**【在 Bash 执行】**

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git curl jq tmux
```

### 1.2 Python 环境

**【在 Bash 执行】**

```bash
mkdir -p ~/ai-stack
cd ~/ai-stack
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install litellm openai
# 可选：如果你希望把 API Key 放到 .env 再加载，再安装
# pip install python-dotenv
```

这几个包分别做什么：

- `litellm`：本地模型网关进程（你要跑 `litellm --config ...`，所以必须装）。
- `openai`：示例 Python 代理代码里用 OpenAI 兼容客户端访问本地网关（必须装）。
- `python-dotenv`：**可选**。只有当你想把 Key 写在 `.env` 文件、再由脚本自动加载时才需要；按本文当前命令用 `export` 的方式，不装也可以。


---

## 2) 创建目录结构

**【在 Bash 执行】**

```bash
cd ~/ai-stack
mkdir -p openclaw orchestrator/agents kimi_solo
```

你最终会有：

```text
~/ai-stack/
  .venv/
  openclaw/
    config.yaml
  litellm.yaml
  kimi_solo/
    kimi_agent.py
  orchestrator/
    master.py
    router.py
    agents/
      code_agent.py
      review_agent.py
      research_agent.py
      doc_agent.py
```

---

## 3) 配置模型网关（LiteLLM）

> 你的两个会员（Kimi / Qwen）先统一接入网关，再给 OpenClaw 和 Python 代理共同使用。

### 3.1 创建网关配置

**【打开文件编辑】** `~/ai-stack/litellm.yaml`

```yaml
model_list:
  # ===== Kimi 独立模型（只给 kimi_solo 使用） =====
  - model_name: kimi-solo
    litellm_params:
      model: openai/moonshot-v1-8k
      api_base: https://api.moonshot.cn/v1
      api_key: ${KIMI_API_KEY}

  # ===== Qwen 多代理模型 =====
  - model_name: qwen-master
    litellm_params:
      model: openai/qwen-max
      api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_key: ${QWEN_API_KEY}

  - model_name: qwen-code
    litellm_params:
      model: openai/qwen-coder-plus
      api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_key: ${QWEN_API_KEY}

  - model_name: qwen-review
    litellm_params:
      model: openai/qwen-plus
      api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_key: ${QWEN_API_KEY}

  - model_name: qwen-doc
    litellm_params:
      model: openai/qwen-plus
      api_base: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_key: ${QWEN_API_KEY}
```

### 3.2 启动网关

**【在 Bash 执行】**

```bash
cd ~/ai-stack
source .venv/bin/activate
export KIMI_API_KEY="你的Kimi Key"
export QWEN_API_KEY="你的Qwen Key"
litellm --config ./litellm.yaml --port 4000
```

> 这个终端不要关，后面都依赖它。

---

## 4) 配置 OpenClaw（两套 profile）

下面给你一个“通用 OpenAI 兼容接口”配置思路：  
- `kimi_solo`：固定 `kimi-solo` 模型，不给路由能力  
- `qwen_orchestrator`：主模型 + 子模型清单，用于多代理

### 4.1 创建 OpenClaw 配置文件

**【打开文件编辑】** `~/ai-stack/openclaw/config.yaml`

```yaml
providers:
  litellm_local:
    type: openai-compatible
    base_url: http://127.0.0.1:4000
    api_key: dummy

profiles:
  kimi_solo:
    provider: litellm_local
    model: kimi-solo
    system_prompt: |
      你是独立的 Kimi 工程代理。
      只能使用 kimi-solo。
      输出可执行步骤，不进行模型切换。
    tools: []

  qwen_orchestrator:
    provider: litellm_local
    model: qwen-master
    system_prompt: |
      你是主控代理（Master）。
      你需要根据任务类型分派到 code/review/research/doc 子代理。
      先路由，再校验，再汇总。
    subagents:
      code_agent:
        model: qwen-code
      review_agent:
        model: qwen-review
      research_agent:
        model: qwen-master
      doc_agent:
        model: qwen-doc
```

> 注意：不同 OpenClaw 版本字段名可能略有区别（如 `agents` / `workers` / `subagents`）。  
> 如果启动报字段错误，按 `openclaw --help` 或官方样例把字段名替换掉，结构保持不变即可。

### 4.2 用该配置启动 OpenClaw

**【在 Bash 执行】**

```bash
cd ~/ai-stack
openclaw --config ~/ai-stack/openclaw/config.yaml
```

---

## 5) 补一套 Python 版本（用于你排查与对照）

> 这套不是必须，但非常建议保留：当 OpenClaw 配置有问题时，你可以先用 Python 脚本验证模型链路。

### 5.1 Kimi 单体

**【打开文件编辑】** `~/ai-stack/kimi_solo/kimi_agent.py`

```python
from openai import OpenAI

client = OpenAI(api_key="dummy", base_url="http://127.0.0.1:4000")


def run_kimi_solo(user_task: str) -> str:
    resp = client.chat.completions.create(
        model="kimi-solo",
        messages=[
            {"role": "system", "content": "你只能使用 kimi-solo，并输出可执行步骤。"},
            {"role": "user", "content": user_task},
        ],
        temperature=0.2,
    )
    return resp.choices[0].message.content


if __name__ == "__main__":
    print(run_kimi_solo("给我一个 Ubuntu 下批量重命名文件的脚本"))
```

### 5.2 Qwen 路由器

**【打开文件编辑】** `~/ai-stack/orchestrator/router.py`

```python
def route_task(task: str) -> str:
    t = task.lower()
    if any(k in t for k in ["code", "coding", "代码", "bug", "脚本"]):
        return "code_agent"
    if any(k in t for k in ["review", "审查", "测试", "性能", "安全"]):
        return "review_agent"
    if any(k in t for k in ["research", "调研", "解释", "对比"]):
        return "research_agent"
    return "doc_agent"
```

### 5.3 Qwen 子代理

**【打开文件编辑】** `~/ai-stack/orchestrator/agents/code_agent.py`

```python
from openai import OpenAI
client = OpenAI(api_key="dummy", base_url="http://127.0.0.1:4000")

def run(task: str) -> str:
    r = client.chat.completions.create(model="qwen-code", messages=[{"role": "user", "content": task}], temperature=0.2)
    return r.choices[0].message.content
```

**【打开文件编辑】** `~/ai-stack/orchestrator/agents/review_agent.py`

```python
from openai import OpenAI
client = OpenAI(api_key="dummy", base_url="http://127.0.0.1:4000")

def run(task: str) -> str:
    r = client.chat.completions.create(model="qwen-review", messages=[{"role": "user", "content": task}], temperature=0.2)
    return r.choices[0].message.content
```

**【打开文件编辑】** `~/ai-stack/orchestrator/agents/research_agent.py`

```python
from openai import OpenAI
client = OpenAI(api_key="dummy", base_url="http://127.0.0.1:4000")

def run(task: str) -> str:
    r = client.chat.completions.create(model="qwen-master", messages=[{"role": "user", "content": task}], temperature=0.3)
    return r.choices[0].message.content
```

**【打开文件编辑】** `~/ai-stack/orchestrator/agents/doc_agent.py`

```python
from openai import OpenAI
client = OpenAI(api_key="dummy", base_url="http://127.0.0.1:4000")

def run(task: str) -> str:
    r = client.chat.completions.create(model="qwen-doc", messages=[{"role": "user", "content": task}], temperature=0.2)
    return r.choices[0].message.content
```

### 5.4 Qwen 主控

**【打开文件编辑】** `~/ai-stack/orchestrator/master.py`

```python
import json
from router import route_task
from agents import code_agent, review_agent, research_agent, doc_agent


def dispatch(task: str) -> dict:
    target = route_task(task)
    runner = {
        "code_agent": code_agent.run,
        "review_agent": review_agent.run,
        "research_agent": research_agent.run,
        "doc_agent": doc_agent.run,
    }[target]
    raw = runner(task)
    return {
        "summary": f"任务已路由到 {target}",
        "actions": ["route", "execute", "merge"],
        "risks": ["建议人工复核关键命令"],
        "next_step": "如需可继续拆子任务",
        "raw": raw,
    }


if __name__ == "__main__":
    print(json.dumps(dispatch("写一个 FastAPI hello world 并附带项目目录"), ensure_ascii=False, indent=2))
```

---

## 6) 最小验证闭环（你现在就能做）

### 6.1 验证 LiteLLM 是否在线

**【在 Bash 执行】**

```bash
curl -s http://127.0.0.1:4000/v1/models | jq .
```

### 6.2 验证 Kimi 单体

**【在 Bash 执行】**

```bash
cd ~/ai-stack
source .venv/bin/activate
python kimi_solo/kimi_agent.py
```

### 6.3 验证 Qwen 多代理

**【在 Bash 执行】**

```bash
cd ~/ai-stack
source .venv/bin/activate
python orchestrator/master.py
```

### 6.4 验证 OpenClaw 两套 profile

**【在 Bash 执行】**

```bash
# 示例命令，具体子命令按你的 openclaw 版本调整
openclaw --config ~/ai-stack/openclaw/config.yaml run --profile kimi_solo --input "写一个shell备份脚本"
openclaw --config ~/ai-stack/openclaw/config.yaml run --profile qwen_orchestrator --input "实现并审查一个python日志模块"
```

---

## 7) 你的原始要求，是否完整满足？

是，且现在分成了两条独立链路：

- **链路 A（Kimi）**：`kimi_solo` profile + `kimi-solo` model，确保独立、单模型。
- **链路 B（Qwen）**：`qwen_orchestrator` profile + `qwen-master` + subagents，实现主模型调度多模型。

如果你按上面 6.1~6.4 都跑通，就说明“你要的两套系统”已经完整落地。

---

## 8) 常见问题（你现在最可能踩）

1. **OpenClaw 字段名和本文不一致**：先看 `openclaw --help` 和样例，把字段名对齐。  
2. **网关能起但模型报 401**：通常是 `KIMI_API_KEY/QWEN_API_KEY` 没 export 到当前终端。  
3. **Kimi 被路由走了 Qwen**：说明把 Kimi 挂进 orchestrator 了，需保持 profile 独立。  
4. **主模型输出漂移**：把温度压低（0.1~0.3），并强制 JSON 输出。


---

## 9) 回滚方案（出问题时不用重装系统）

### 9.1 停掉临时进程

**【在 Bash 执行】**

```bash
pkill -f "litellm --config" || true
pkill -f "openclaw --config" || true
```

### 9.2 恢复 OpenClaw 配置备份

**【在 Bash 执行】**

```bash
# 先看可用备份
ls -lah ~/ai-stack/backup/
# 恢复一个备份（示例）
cp -av ~/ai-stack/backup/config.yaml.bak.YYYY-MM-DD-HHMMSS ~/ai-stack/openclaw/config.yaml
```

### 9.3 清理本次 Python 环境（可选）

**【在 Bash 执行】**

```bash
rm -rf ~/ai-stack/.venv
python3 -m venv ~/ai-stack/.venv
```

这样你可以只重建 Python 依赖，不需要重装 Ubuntu。
