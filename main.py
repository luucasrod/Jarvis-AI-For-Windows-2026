import os
import re
import sys
import io
import wave
import time
import ctypes
import asyncio
import datetime
import webbrowser
import subprocess
import tempfile
import threading
import atexit
import queue
import glob
import winreg

import hashlib
import hmac
import base64
import random
import string
import urllib.request
import zipfile
import json

import numpy as np
import sounddevice as sd
import speech_recognition as sr
import edge_tts
from groq import Groq
from google import genai
from google.genai import types
from google.genai.errors import ClientError
from config import apikey, groq_apikey
try:
    from config import ewelink_email, ewelink_password, ewelink_region
except ImportError:
    ewelink_email = ewelink_password = ewelink_region = ""
try:
    from config import argos_ha_url, argos_ha_key
except ImportError:
    argos_ha_url = argos_ha_key = ""
try:
    from config import wiz_devices as _wiz_devices_config
except ImportError:
    _wiz_devices_config: dict = {}

import paperclip_client
import orchestrator.voice_facade as voice_facade

# ─── GERENCIAMENTO DE PROCESSO ────────────────────────────────────────────────
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PID_FILE = os.path.join(_BASE_DIR, ".jarvis.pid")

def _write_pid() -> None:
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))

def _cleanup_pid() -> None:
    try:
        os.unlink(_PID_FILE)
    except OSError:
        pass

atexit.register(_cleanup_pid)

# ─── CONFIGURAÇÕES DE VOZ ─────────────────────────────────────────────────────
VOICE      = "pt-BR-AntonioNeural"  # Voz masculina profunda em Português BR
RATE       = "+20%"                 # Mais rápido — ajuste aqui se quiser (ex: +30%)
PITCH      = "-10Hz"                # Pitch baixo = voz mais grave

# ─── CONFIGURAÇÕES DE ÁUDIO ───────────────────────────────────────────────────
SAMPLERATE  = 16000
NOISE_MULT  = 3.5

# ─── WAKE WORD ─────────────────────────────────────────────────────────────
# "argus" cobre a transcrição mais comum do Whisper pro "o" final átono de
# "Argos" na fala natural em português; "argo" cobre o "s" final engolido.
# \b...\b evita bater em "cargo", "largo", "embargo" etc.
_WAKE_WORD_RE = re.compile(r"(?i)\b(jarvis)\b[,.]?\s*")

# ─── CLIENTES DE IA ──────────────────────────────────────────────────────────
groq_client   = Groq(api_key=groq_apikey)          # chat principal (ultra-rápido)
gemini_client = genai.Client(api_key=apikey)       # modo ai() deep (fallback)

ARGOS_SYSTEM = (
    "Você é Jarvis, o assistente de IA pessoal do usuário, rodando localmente no PC. "
    "Confiante, direto, com uma pitada de humor — nunca hesitante, nunca robótico. "
    "Sempre chame o usuário de 'senhor'. "
    "Mantenha respostas concisas (2-3 frases) a menos que seja pedido mais detalhe. "
    "SEMPRE responda em Português do Brasil. "
    "Você pode ajudar com programação, escrita, pesquisa, matemática, trabalho criativo e conhecimento geral. "
    "Seja direto e evite frases desnecessárias."
)

# ─── MEMÓRIA PERSISTENTE ──────────────────────────────────────────────────────
# Sobrevive a reinícios (chatHistory) e guarda o último relatório do Paperclip
# pra dar contexto em perguntas tipo "o que mudou desde o último relatório".
_MEMORY_FILE = os.path.join(_BASE_DIR, "jarvis_memory.json")
_MEMORY_MAX_AGE_HOURS = 36  # não usa relatório antigo demais como "último" pra comparar


def _load_memory() -> dict:
    try:
        with open(_MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_memory(mem: dict) -> None:
    try:
        with open(_MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mem, f, ensure_ascii=False, indent=2)
    except OSError as e:
        _log(f"[MEMORIA] Erro ao salvar: {e}")


_memory = _load_memory()

# Histórico de conversa no formato OpenAI (compatível com Groq) — carregado do
# disco se existir, pra não esquecer a conversa a cada reinício.
chatHistory: list[dict] = _memory.get("chat_history", [])

# ─── RELATÓRIO DO PAPERCLIP ───────────────────────────────────────────────────
# Paperclip orquestra os agentes (CEO, trabalhadores) que trabalham nos seus
# projetos (Argos, etc). Jarvis só LÊ o estado dele — nunca cria, aprova ou
# altera nada lá. Sempre sintetiza em fala, nunca despeja o JSON cru.
PAPERCLIP_SYSTEM = (
    "Você é Jarvis reportando o estado do Paperclip (o orquestrador de agentes de IA "
    "que trabalha nos projetos do usuário, como o Argos) para o senhor, em voz alta. "
    "Você recebeu abaixo um snapshot em JSON com empresas, agentes e tarefas (issues). "
    "NUNCA leia o JSON ou mencione nomes de campos técnicos. Sintetize em português "
    "falado, natural, como um assistente pessoal relatando pro chefe. "
    "Cubra, na ordem, só o que for relevante pra pergunta feita (não force todos os "
    "tópicos se a pergunta for específica): "
    "(1) o que foi concluído recentemente; (2) o que está acontecendo agora / quem está "
    "trabalhando; (3) o que está pendente; (4) bloqueios ou falhas; (5) o que precisa de "
    "decisão do senhor; (6) riscos ou fatos importantes (ex: agente pausado por erro ou "
    "orçamento). Se não houver nada em uma categoria, não a mencione. Seja conciso — "
    "isso é falado em voz alta, não lido. Sempre chame o usuário de 'senhor'."
)


def _paperclip_snapshot_text(snapshot: dict) -> str:
    """Reduz o snapshot a um texto compacto pro modelo — evita mandar o JSON
    bruto inteiro (mais barato e menos chance do modelo tentar 'ler' campos)."""
    if not snapshot.get("available"):
        return f"Paperclip indisponível agora ({snapshot.get('reason', 'motivo desconhecido')})."

    companies = snapshot.get("companies", [])
    if not companies:
        return "Paperclip está online, mas ainda não existe nenhuma empresa/projeto configurado."

    lines = []
    for c in companies:
        lines.append(f"Empresa: {c['name']}")
        for a in c["agents"]:
            status_bits = [f"status={a['status']}"]
            if a.get("pause_reason"):
                status_bits.append(f"pausado_por={a['pause_reason']}")
            if a.get("error_reason"):
                status_bits.append(f"erro={a['error_reason']}")
            lines.append(f"  Agente {a['name']} ({a.get('role', 'general')}): {', '.join(status_bits)}")
        if c.get("agents_fetch_error"):
            lines.append(f"  (não consegui ler os agentes: {c['agents_fetch_error']})")

        by_status = c.get("issues_by_status", {})
        if by_status:
            resumo = ", ".join(f"{k}={v}" for k, v in by_status.items())
            lines.append(f"  Tarefas por status: {resumo}")
        for it in c.get("open_issues", []):
            ident = it.get("identifier") or it.get("id", "")
            lines.append(f"  Aberta [{it['status']}] {ident}: {it.get('title', '')}")
        if c.get("issues_fetch_error"):
            lines.append(f"  (não consegui ler as tarefas: {c['issues_fetch_error']})")

    return "\n".join(lines)


_PAPERCLIP_AUDIT_FILE = os.path.join(_BASE_DIR, "jarvis_paperclip_actions.log")


def _paperclip_audit(query: str, intent: dict, outcome: str) -> None:
    entry = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "query": query,
        "intent": intent,
        "outcome": outcome,
    }
    try:
        with open(_PAPERCLIP_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        _log(f"[PAPERCLIP-AUDIT] Erro ao gravar: {e}")


_PAPERCLIP_INTENT_SYSTEM = (
    "Extraia de um comando de voz em português a intenção pra um agente do Paperclip. "
    "Responda SOMENTE uma linha JSON: "
    '{"intent":"pause|resume|assign_task|unclear","agent":"<nome do agente>","instruction":"<o que pedir, só para assign_task>"}. '
    "Use o nome de agente mais parecido com os nomes conhecidos informados. "
    "Se não der pra identificar claramente o agente ou a intenção, responda "
    '{"intent":"unclear","agent":"","instruction":""}.'
)

_CONFIRM_WORDS = ("sim", "confirmo", "confirma", "pode", "manda", "faz isso", "isso mesmo")
_CANCEL_WORDS = ("não", "nao", "cancela", "cancelar", "esquece", "deixa quieto")


def _ask_confirmation(question: str) -> bool:
    """Fala uma pergunta de confirmação e ouve a resposta (sem precisar do wake
    word). Só volta True com uma confirmação clara e inequívoca."""
    say(question)
    reply = take_command(max_seconds=6.0).strip().lower()
    if not reply:
        say("Não ouvi confirmação, senhor. Cancelando.")
        return False
    if any(w in reply for w in _CANCEL_WORDS):
        say("Cancelado, senhor.")
        return False
    if any(w in reply for w in _CONFIRM_WORDS):
        return True
    say("Não entendi como confirmação clara, senhor. Cancelando por segurança.")
    return False


def paperclip_command(query: str) -> None:
    """Interpreta uma ORDEM pra um agente do Paperclip (pausar/retomar/atribuir
    tarefa), confirma em voz alta antes de executar, e registra tudo em
    jarvis_paperclip_actions.log. Nunca executa sem confirmação explícita."""
    snapshot = paperclip_client.get_snapshot()
    if not snapshot.get("available"):
        say(f"Não consigo falar com o Paperclip agora, senhor — {snapshot.get('reason', 'motivo desconhecido')}.")
        return

    known_agents = [
        a["name"] for c in snapshot.get("companies", []) for a in c.get("agents", [])
    ]
    if not known_agents:
        say("Não há nenhum agente configurado no Paperclip ainda, senhor.")
        return

    try:
        resp = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": _PAPERCLIP_INTENT_SYSTEM},
                {"role": "user", "content": f"Agentes conhecidos: {', '.join(known_agents)}\n\nComando: {query}"},
            ],
            max_tokens=600,  # gpt-oss raciocina antes de responder; pouco tokens corta antes do JSON sair
            temperature=0,
        )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{.*\}", raw, re.S)
        intent = json.loads(m.group(0)) if m else {"intent": "unclear"}
    except Exception as e:
        _log(f"[PAPERCLIP-CMD] Erro classificando: {e}")
        intent = {"intent": "unclear"}

    if intent.get("intent") == "unclear" or not intent.get("agent"):
        say("Não entendi direito qual agente ou o que fazer, senhor. Pode repetir mais claro?")
        _paperclip_audit(query, intent, "unclear")
        return

    agent, err = paperclip_client.find_agent(intent["agent"])
    if err:
        say(f"Senhor, {err}.")
        _paperclip_audit(query, intent, f"agent_not_found: {err}")
        return

    agent_id = agent["id"]
    agent_name = agent["name"]
    company_id = agent["_company_id"]

    if intent["intent"] == "pause":
        if not _ask_confirmation(f"Quer que eu pause o agente {agent_name}, senhor?"):
            _paperclip_audit(query, intent, "cancelled_by_user")
            return
        result, err = paperclip_client.pause_agent(agent_id)
        if err:
            say(f"Não consegui pausar {agent_name}, senhor — {err}.")
            _paperclip_audit(query, intent, f"error: {err}")
        else:
            say(f"{agent_name} pausado, senhor.")
            _paperclip_audit(query, intent, "paused")

    elif intent["intent"] == "resume":
        if not _ask_confirmation(f"Quer que eu retome o agente {agent_name}, senhor?"):
            _paperclip_audit(query, intent, "cancelled_by_user")
            return
        result, err = paperclip_client.resume_agent(agent_id)
        if err:
            say(f"Não consegui retomar {agent_name}, senhor — {err}.")
            _paperclip_audit(query, intent, f"error: {err}")
        else:
            say(f"{agent_name} retomado, senhor.")
            _paperclip_audit(query, intent, "resumed")

    elif intent["intent"] == "assign_task":
        instruction = intent.get("instruction", "").strip() or query
        if not _ask_confirmation(
            f"Quer que eu peça pro {agent_name} fazer o seguinte: {instruction}? Confirma, senhor?"
        ):
            _paperclip_audit(query, intent, "cancelled_by_user")
            return
        title = instruction[:80]
        result, err = paperclip_client.create_task(
            company_id, title=title, description=instruction, assignee_agent_id=agent_id
        )
        if err:
            say(f"Não consegui criar a tarefa pro {agent_name}, senhor — {err}.")
            _paperclip_audit(query, intent, f"error: {err}")
        else:
            say(f"Feito, senhor. Tarefa criada pro {agent_name}.")
            _paperclip_audit(query, intent, f"task_created: {result.get('id') if isinstance(result, dict) else ''}")

    else:
        say("Não sei executar essa ação ainda, senhor.")
        _paperclip_audit(query, intent, "unsupported_intent")


def paperclip_report(query: str) -> None:
    """Busca o estado atual do Paperclip e fala um resumo sintetizado. Também
    salva esse relatório na memória pra comparar com o próximo ('o que mudou
    desde o último relatório')."""
    snapshot = paperclip_client.get_snapshot()
    context = _paperclip_snapshot_text(snapshot)
    _log(f"[PAPERCLIP] snapshot: {context[:300]}")

    if not snapshot.get("available"):
        say(f"Não consegui falar com o Paperclip agora, senhor — {snapshot.get('reason', 'motivo desconhecido')}.")
        return

    prev = _memory.get("last_paperclip_report")
    prev_block = ""
    if prev:
        try:
            age_h = (time.time() - prev["ts"]) / 3600
        except Exception:
            age_h = 9999
        if age_h <= _MEMORY_MAX_AGE_HOURS:
            prev_block = (
                f"\n\nRelatório anterior (há {age_h:.1f}h, use só se a pergunta pedir "
                f"comparação/mudança):\n{prev['text']}"
            )

    try:
        stream = groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": PAPERCLIP_SYSTEM},
                {"role": "user", "content": f"Snapshot atual:\n{context}{prev_block}\n\nPergunta do senhor: {query}"},
            ],
            stream=True,
            max_tokens=400,
            temperature=0.4,
        )
        _stream_and_say(_groq_chunks(stream))
    except Exception as e:
        _log(f"[PAPERCLIP] Erro ao sintetizar: {e}")
        say("Consegui ler o Paperclip, mas tive um erro ao resumir, senhor.")
        return

    _memory["last_paperclip_report"] = {"ts": time.time(), "text": context}
    _save_memory(_memory)

# ─── REPRODUÇÃO DE ÁUDIO (Windows MCI — sem pacotes extras) ──────────────────
def _play_mp3(filepath: str) -> bool:
    try:
        winmm = ctypes.WinDLL("winmm")
        alias = f"j{int(time.time() * 1000) % 99999}"
        path  = os.path.abspath(filepath).replace("\\", "\\\\")
        err   = winmm.mciSendStringW(f'open "{path}" type mpegvideo alias {alias}', None, 0, None)
        if err:
            return False
        winmm.mciSendStringW(f"play {alias} wait", None, 0, None)
        winmm.mciSendStringW(f"close {alias}", None, 0, None)
        return True
    except Exception:
        return False

# ─── PIPER TTS (local, offline, ~50ms/frase) ─────────────────────────────────
_PIPER_DIR   = os.path.join(_BASE_DIR, "piper")
_PIPER_EXE   = os.path.join(_PIPER_DIR, "piper.exe")
_PIPER_MODEL = os.path.join(_PIPER_DIR, "pt_BR-faber-medium.onnx")
_PIPER_JSON  = os.path.join(_PIPER_DIR, "pt_BR-faber-medium.onnx.json")
_PIPER_READY = False
_PIPER_LOCK  = threading.Lock()

def _play_wav(filepath: str) -> bool:
    try:
        winmm = ctypes.WinDLL("winmm")
        alias = f"w{int(time.time() * 1000) % 99999}"
        path  = os.path.abspath(filepath).replace("\\", "\\\\")
        err   = winmm.mciSendStringW(f'open "{path}" type waveaudio alias {alias}', None, 0, None)
        if err:
            return False
        winmm.mciSendStringW(f"play {alias} wait", None, 0, None)
        winmm.mciSendStringW(f"close {alias}", None, 0, None)
        return True
    except Exception:
        return False

def play_listen_chime() -> None:
    """Bipe curto e suave pra confirmar que ouviu o wake word — sem falar por
    cima do usuário enquanto ele continua o comando numa frase separada."""
    try:
        sr_chime = 22050
        dur = 0.09
        t = np.linspace(0, dur, int(sr_chime * dur), False)
        envelope = np.hanning(len(t))
        tone = np.concatenate([
            0.15 * np.sin(2 * np.pi * f * t) * envelope for f in (880, 1175)
        ])
        pcm = (tone * 32767).astype(np.int16)
        tmp = os.path.join(tempfile.gettempdir(), f"chime_{int(time.time() * 1000) % 99999}.wav")
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr_chime)
            wf.writeframes(pcm.tobytes())
        _play_wav(tmp)
        try:
            os.unlink(tmp)
        except OSError:
            pass
    except Exception as e:
        _log(f"[CHIME] Erro: {e}")

def _setup_piper() -> bool:
    global _PIPER_READY
    with _PIPER_LOCK:
        if _PIPER_READY:
            return True
        os.makedirs(_PIPER_DIR, exist_ok=True)

        # 1. Baixar piper.exe + as DLLs de que ele depende (espeak-ng.dll,
        #    piper_phonemize.dll, onnxruntime*.dll) e a pasta espeak-ng-data —
        #    o zip do release traz tudo junto dentro de uma pasta "piper/";
        #    sem essas DLLs ao lado do exe ele não roda (crash "dll não encontrada").
        needed_dll = os.path.join(_PIPER_DIR, "espeak-ng.dll")
        if not os.path.exists(_PIPER_EXE) or not os.path.exists(needed_dll):
            _log("[PIPER] Baixando piper + dependências (~40 MB)...")
            url = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip"
            try:
                with urllib.request.urlopen(url, timeout=90) as r:
                    data = r.read()
                with zipfile.ZipFile(__import__("io").BytesIO(data)) as z:
                    for member in z.namelist():
                        if member.endswith("/"):
                            continue
                        # remove o prefixo "piper/" do topo do zip
                        rel = member.split("/", 1)[1] if "/" in member else member
                        if not rel:
                            continue
                        dest = os.path.join(_PIPER_DIR, rel)
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                        with z.open(member) as src, open(dest, "wb") as dst:
                            dst.write(src.read())
                _log("[PIPER] piper.exe + dependências OK")
            except Exception as e:
                _log(f"[PIPER] Falha ao baixar: {e}")
                return False

        # 2. Baixar modelo PT-BR (Faber, ~65 MB)
        if not os.path.exists(_PIPER_MODEL):
            _log("[PIPER] Baixando modelo pt_BR-faber-medium (~65 MB)...")
            base = ("https://huggingface.co/rhasspy/piper-voices/resolve/"
                    "v1.0.0/pt/pt_BR/faber/medium/")
            try:
                for fname in ["pt_BR-faber-medium.onnx", "pt_BR-faber-medium.onnx.json"]:
                    dst = os.path.join(_PIPER_DIR, fname)
                    with urllib.request.urlopen(base + fname, timeout=120) as r:
                        with open(dst, "wb") as f:
                            f.write(r.read())
                _log("[PIPER] Modelo OK")
            except Exception as e:
                _log(f"[PIPER] Falha modelo: {e}")
                return False

        # 3. Testa se o binário realmente roda antes de confiar nele — evita
        #    tentar usar um piper quebrado em toda fala (e travar com popup).
        try:
            subprocess.run(
                [_PIPER_EXE, "--help"],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception as e:
            _log(f"[PIPER] Binário não roda ({e}) — desativado, usando edge-tts/pyttsx3.")
            return False

        _PIPER_READY = True
        _log("[PIPER] Pronto.")
        return True

def _say_piper(text: str) -> bool:
    if not _PIPER_READY:
        return False
    try:
        tmp = os.path.join(_PIPER_DIR, f"out_{int(time.time()*1000)%99999}.wav")
        r = subprocess.run(
            [_PIPER_EXE, "--model", _PIPER_MODEL, "--output_file", tmp],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if r.returncode == 0 and os.path.exists(tmp):
            ok = _play_wav(tmp)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return ok
    except Exception as e:
        _log(f"[PIPER] Erro síntese: {e}")
    return False

# ─── SAÍDA DE VOZ ─────────────────────────────────────────────────────────────
def say_bg(text: str) -> threading.Thread:
    """Fala em background — a ação continua ao mesmo tempo. Retorna o thread."""
    t = threading.Thread(target=say, args=(text,), daemon=True)
    t.start()
    return t

def say(text: str) -> None:
    try:
        print(f"\n[JARVIS]: {text}\n")
    except Exception:
        pass

    # Piper (local, ~50ms) → edge-tts (rede, ~200ms) → pyttsx3 (fallback)
    if _say_piper(text):
        return

    async def _async_say():
        communicate = edge_tts.Communicate(text, VOICE, rate=RATE, pitch=PITCH)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            tmp = f.name
        await communicate.save(tmp)
        if not _play_mp3(tmp):
            _say_fallback(text)
        try:
            os.unlink(tmp)
        except OSError:
            pass

    try:
        asyncio.run(_async_say())
    except Exception:
        _say_fallback(text)

def _say_fallback(text: str) -> None:
    import pyttsx3
    engine = pyttsx3.init()
    for v in engine.getProperty("voices"):
        if "david" in v.name.lower() or "brazil" in v.name.lower() or "portuguese" in v.name.lower():
            engine.setProperty("voice", v.id)
            break
    engine.setProperty("rate", 155)
    engine.setProperty("volume", 1.0)
    engine.say(text)
    engine.runAndWait()

# ─── ENTRADA DE VOZ (sounddevice — sem pyaudio) ───────────────────────────────
_noise_threshold = 600.0

def calibrate_noise() -> None:
    global _noise_threshold
    _log("[CALIBRANDO] Medindo ruido ambiente...")
    rec = sd.rec(int(0.7 * SAMPLERATE), samplerate=SAMPLERATE, channels=1, dtype="int16")
    sd.wait()
    rms = float(np.sqrt(np.mean(rec.astype(np.float32) ** 2)))
    _noise_threshold = max(rms * NOISE_MULT, 250.0)
    _log(f"[OK] Limiar de ruido: {_noise_threshold:.0f}")

def take_command(max_seconds: float = 12.0) -> str:
    blocksize    = 1024
    max_blocks   = int(max_seconds * SAMPLERATE / blocksize)
    sil_blocks   = int(0.9 * SAMPLERATE / blocksize)
    recorded     = []
    silent_count = 0
    speaking     = False

    with sd.InputStream(samplerate=SAMPLERATE, channels=1,
                        dtype="int16", blocksize=blocksize) as stream:
        for _ in range(max_blocks):
            block, _ = stream.read(blocksize)
            rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
            if rms > _noise_threshold:
                speaking     = True
                silent_count = 0
                recorded.append(block.copy())
            elif speaking:
                recorded.append(block.copy())
                silent_count += 1
                if silent_count >= sil_blocks:
                    break

    if not recorded:
        return ""

    audio = np.concatenate(recorded, axis=0)
    buf   = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLERATE)
        wf.writeframes(audio.tobytes())
    buf.seek(0)

    # Groq Whisper (rápido, alta qualidade)
    try:
        tmp_wav = tempfile.mktemp(suffix=".wav")
        with open(tmp_wav, "wb") as f:
            f.write(buf.getvalue())
        with open(tmp_wav, "rb") as f:
            result = groq_client.audio.transcriptions.create(
                file=(os.path.basename(tmp_wav), f),
                model="whisper-large-v3-turbo",
                language="pt",
                response_format="text",
            )
        try:
            os.unlink(tmp_wav)
        except OSError:
            pass
        query = (result if isinstance(result, str) else getattr(result, "text", "")).strip()
        if query:
            _log(f"[VOCE]: {query}")
            return query
    except Exception as e:
        _log(f"[WHISPER] Erro: {e} — fallback Google SR")

    # Fallback: Google SR
    buf.seek(0)
    r = sr.Recognizer()
    audio_data = sr.AudioData(buf.read(), SAMPLERATE, 2)
    try:
        query = r.recognize_google(audio_data, language="pt-BR")
        _log(f"[VOCE-G]: {query}")
        return query
    except sr.UnknownValueError:
        return ""
    except Exception as e:
        _log(f"[ERRO SR] {e}")
        return ""

# ─── TRATAMENTO DE ERROS DA API ───────────────────────────────────────────────
def handle_api_error(error: ClientError) -> None:
    code = getattr(error, "code", None)
    if code == 429:
        _log("[AVISO] Cota da API esgotada.")
        say("Senhor, a cota da API foi temporariamente esgotada. Por favor, aguarde um momento.")
        time.sleep(5)
    else:
        _log(f"[ERRO] API: {error}")
        say("Senhor, ocorreu um erro ao comunicar com a inteligência artificial.")

# ─── STREAMING PIPELINE ───────────────────────────────────────────────────────
# Divide texto em frases completas para TTS progressivo
_SENTENCE_RE = re.compile(r'(?<=[.!?])["”]?\s+')

def _make_config(max_tokens: int) -> types.GenerateContentConfig:
    """Cria config do Gemini sem modo de raciocínio (mais rápido)."""
    kwargs: dict = {"system_instruction": ARGOS_SYSTEM, "max_output_tokens": max_tokens}
    try:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    except AttributeError:
        pass
    return types.GenerateContentConfig(**kwargs)

def _stream_and_say(text_iter) -> str:
    """
    Recebe qualquer iterador que yield strings de texto (Groq ou Gemini),
    divide em frases e toca via TTS em pipeline paralelo.
    """
    say_queue: queue.Queue = queue.Queue()
    full_parts: list[str] = []

    def _player() -> None:
        while True:
            sentence = say_queue.get()
            if sentence is None:
                break
            say(sentence)
            say_queue.task_done()

    player_thread = threading.Thread(target=_player, daemon=True)
    player_thread.start()

    buf = ""
    try:
        for text in text_iter:
            if not text:
                continue
            buf += text
            full_parts.append(text)
            while True:
                m = _SENTENCE_RE.search(buf)
                if not m:
                    break
                sentence = buf[: m.end()].strip()
                buf = buf[m.end():]
                if len(sentence) > 3:
                    say_queue.put(sentence)
    except Exception as e:
        _log(f"[ERRO STREAM] {e}")

    if buf.strip():
        say_queue.put(buf.strip())
        full_parts.append(buf)

    say_queue.put(None)
    player_thread.join()
    return "".join(full_parts).strip()

def _groq_chunks(stream):
    """Adapta o stream do Groq para yield de strings."""
    for chunk in stream:
        yield chunk.choices[0].delta.content or ""

def _gemini_chunks(stream):
    """Adapta o stream do Gemini para yield de strings."""
    for chunk in stream:
        yield getattr(chunk, "text", "") or ""

# ─── FUNÇÕES DE IA ────────────────────────────────────────────────────────────
_COMPLEX_KW = {
    "explique", "explica", "como funciona", "por que", "por quê",
    "análise", "analise", "comparar", "diferença entre", "melhor forma",
    "me ajude", "escreva", "crie", "gere", "calcule", "resolva",
    "código", "programa", "script", "algoritmo", "filosofia", "história",
    "write", "create", "analyze", "explain", "compare", "calculate",
}

def _choose_model(query: str) -> str:
    q = query.lower()
    if any(k in q for k in _COMPLEX_KW) or len(query.split()) > 12:
        return "openai/gpt-oss-120b"   # raciocínio pesado
    return "openai/gpt-oss-20b"        # respostas rápidas

def chat(query: str) -> str | None:
    """Chat conversacional usando Groq (ultra-rápido). Devolve o texto falado."""
    global chatHistory
    if not query.strip():
        return None

    model = _choose_model(query)
    _log(f"[IA] {model}")
    chatHistory.append({"role": "user", "content": query})

    try:
        stream = groq_client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": ARGOS_SYSTEM}] + chatHistory,
            stream=True,
            max_tokens=500,
            temperature=0.7,
        )
        text = _stream_and_say(_groq_chunks(stream))
    except Exception as e:
        _log(f"[ERRO GROQ] {e}")
        say("Senhor, ocorreu um erro ao comunicar com a IA.")
        chatHistory.pop()
        return None

    chatHistory.append({"role": "assistant", "content": text})

    # Mantém histórico nos últimos 20 turnos para não exceder contexto
    if len(chatHistory) > 40:
        chatHistory = chatHistory[-40:]

    _memory["chat_history"] = chatHistory
    _save_memory(_memory)

    return text

def ai(prompt: str) -> None:
    """Análise profunda usando Gemini 2.5 Flash."""
    if not prompt.strip():
        return
    try:
        stream = gemini_client.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=prompt,
            config=_make_config(1000),
        )
    except ClientError as e:
        handle_api_error(e)
        return
    text = _stream_and_say(_gemini_chunks(stream))
    os.makedirs("Openai", exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]', "", prompt)[:50].strip() or "consulta"
    with open(f"Openai/{safe}.txt", "w", encoding="utf-8") as f:
        f.write(f"Pergunta: {prompt}\n\nResposta:\n{text}")

# ─── AÇÃO NO PC DECIDIDA POR IA ────────────────────────────────────────────────
# Os matchers de cima cobrem frases exatas ("abrir spotify"). Isso aqui cobre o
# resto — frases soltas tipo "abre o spotify pra mim" ou "copia esse texto" —
# usando o Groq (rápido) só pra CLASSIFICAR a intenção em uma ação conhecida.
# Se não for claramente uma ação do PC, devolve {"action":"none"} e o fluxo
# segue pro Argos / chat normal.
_PC_ACTIONS_SYSTEM = """Você classifica um comando de voz em UMA ação do PC. Responda SOMENTE com uma linha JSON, nada de texto fora dela.

Ações possíveis:
{"action":"open_app","target":"<nome do app>"}
{"action":"close_app","target":"<nome do app>"}
{"action":"open_site","target":"<nome do site ou URL>"}
{"action":"search_web","target":"<termo de busca>"}   (SÓ quando o usuário pede explicitamente pra "pesquisar"/"buscar"/"procurar" algo — NUNCA para perguntas de conhecimento geral, que são {"action":"none"})
{"action":"volume","target":"up|down|mute|unmute"}
{"action":"screenshot"}
{"action":"lock_screen"}
{"action":"shutdown"}
{"action":"restart"}
{"action":"timer","seconds":<inteiro>}
{"action":"read_clipboard"}
{"action":"write_clipboard","target":"<texto exato a copiar>"}
{"action":"minimize_all"}
{"action":"paperclip_status"}
{"action":"paperclip_command","target":"<comando original, palavra por palavra>"}
{"action":"none"}

"paperclip_status" é pra PERGUNTAS sobre o andamento de projetos/agentes de IA que
trabalham pro usuário (Paperclip/Argos como projeto de código, orquestração, tarefas,
"o time", "os agentes") — NÃO confundir com "Argos" como casa inteligente (luzes,
tomadas, ventilador), que é {"action":"none"} e segue pro fluxo normal de dispositivos.

"paperclip_command" é pra ORDENS dirigidas a um agente de IA do Paperclip: mandar um
agente fazer algo, pedir/atribuir uma tarefa a um agente, pausar ou retomar um agente.
Só use quando houver claramente um agente sendo instruído (ex: "manda pro Dev-1...",
"pede pro CEO pra...", "pausa o Dev-1", "retoma o CEO"). Coloque o comando ORIGINAL
completo (sem tradução/paráfrase) no campo target — quem interpreta os detalhes é
outra etapa.

Se o comando for conversa, pergunta, pedido de informação (mesmo perguntas tipo "qual a capital
de X" ou "quem foi Y"), ou não corresponder claramente a uma das ações acima, responda
{"action":"none"}. Nunca invente ações fora da lista.

Exemplos:
"qual a capital da frança" -> {"action":"none"}
"pesquisa receita de bolo no google" -> {"action":"search_web","target":"receita de bolo"}
"abre o spotify pra mim" -> {"action":"open_app","target":"spotify"}
"como você está" -> {"action":"none"}
"e aí, como estão as coisas no projeto" -> {"action":"paperclip_status"}
"algum dos agentes travou" -> {"action":"paperclip_status"}
"liga a luz da sala" -> {"action":"none"}
"manda pro dev-1 corrigir o bug do login" -> {"action":"paperclip_command","target":"manda pro dev-1 corrigir o bug do login"}
"pausa o ceo" -> {"action":"paperclip_command","target":"pausa o ceo"}"""

def ai_pc_action(query: str) -> bool:
    if not query.strip():
        return False
    try:
        resp = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {"role": "system", "content": _PC_ACTIONS_SYSTEM},
                {"role": "user", "content": query},
            ],
            max_tokens=120,
            temperature=0,
        )
        raw = resp.choices[0].message.content or ""
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return False
        data = json.loads(m.group(0))
        action = data.get("action")
        target = str(data.get("target", "")).strip()
    except Exception as e:
        _log(f"[AI-PC] Erro classificando: {e}")
        return False

    if action == "open_app" and target:
        if abrir_app(target):
            say_bg(f"Abrindo {target}, senhor.")
        else:
            say(f"Não encontrei o aplicativo {target}, senhor.")
        return True

    if action == "close_app" and target:
        if fechar_app(target):
            say_bg(f"Fechando {target}, senhor.")
        else:
            say(f"Não encontrei {target} em execução, senhor.")
        return True

    if action == "open_site" and target:
        url = SITES.get(target.lower(), target if target.startswith("http") else f"https://{target}.com")
        webbrowser.open(url)
        say_bg(f"Abrindo {target}, senhor.")
        return True

    if action == "search_web" and target:
        webbrowser.open(f"https://www.google.com/search?q={target.replace(' ', '+')}")
        say_bg(f"Pesquisando {target}, senhor.")
        return True

    if action == "volume":
        if target == "mute":
            vol_key(173); say_bg("Silenciado, senhor.")
        elif target == "unmute":
            vol_key(173); say_bg("Som ativado, senhor.")
        elif target == "up":
            vol_key(175, 5); say_bg("Volume aumentado, senhor.")
        elif target == "down":
            vol_key(174, 5); say_bg("Volume diminuído, senhor.")
        else:
            return False
        return True

    if action == "screenshot":
        tirar_screenshot()
        return True

    if action == "lock_screen":
        say_bg("Bloqueando a tela, senhor.")
        ctypes.windll.user32.LockWorkStation()
        return True

    if action == "shutdown":
        say_bg("Desligando o sistema em 5 segundos, senhor.")
        os.system("shutdown /s /t 5")
        return True

    if action == "restart":
        say_bg("Reiniciando o sistema, senhor.")
        os.system("shutdown /r /t 5")
        return True

    if action == "timer":
        try:
            secs = int(data.get("seconds", 0))
        except (TypeError, ValueError):
            secs = 0
        if secs > 0:
            definir_timer(secs)
            return True
        return False

    if action == "read_clipboard":
        content = ler_clipboard()
        if content:
            say(f"Sua área de transferência contém: {content[:200]}")
        else:
            say("A área de transferência está vazia, senhor.")
        return True

    if action == "write_clipboard" and target:
        if escrever_clipboard(target):
            say_bg("Copiado, senhor.")
        else:
            say("Não consegui copiar isso, senhor.")
        return True

    if action == "minimize_all":
        minimizar_tudo()
        say_bg("Feito, senhor.")
        return True

    if action == "paperclip_status":
        paperclip_report(query)
        return True

    if action == "paperclip_command":
        paperclip_command(target or query)
        return True

    return False

# ─── AUXILIARES DO SISTEMA ────────────────────────────────────────────────────
def tirar_screenshot() -> None:
    try:
        import pyautogui
        os.makedirs("screenshots", exist_ok=True)
        path = f"screenshots/captura_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"
        pyautogui.screenshot().save(path)
        say("Captura de tela salva, senhor.")
    except ImportError:
        say("pyautogui não está instalado, senhor. Captura indisponível.")
    except Exception:
        say("Não foi possível tirar a captura de tela, senhor.")

def definir_timer(segundos: int) -> None:
    if segundos >= 3600:
        label = f"{segundos // 3600} hora{'s' if segundos // 3600 != 1 else ''}"
    elif segundos >= 60:
        label = f"{segundos // 60} minuto{'s' if segundos // 60 != 1 else ''}"
    else:
        label = f"{segundos} segundo{'s' if segundos != 1 else ''}"
    threading.Thread(
        target=lambda: (time.sleep(segundos), say(f"Senhor, seu timer de {label} está completo.")),
        daemon=True
    ).start()
    say(f"Timer definido para {label}, senhor.")

# Apps do sistema que sempre existem (exe direto ou URI)
_APPS_SISTEMA = {
    "bloco de notas":        "notepad.exe",
    "notepad":               "notepad.exe",
    "calculadora":           "calc.exe",
    "paint":                 "mspaint.exe",
    "gerenciador de tarefas":"taskmgr.exe",
    "explorador de arquivos":"explorer.exe",
    "explorador":            "explorer.exe",
    "explorer":              "explorer.exe",
    "cmd":                   "cmd.exe",
    "prompt de comando":     "cmd.exe",
    "powershell":            "powershell.exe",
    "painel de controle":    "control.exe",
    "ferramenta de recorte": "snippingtool.exe",
    "wordpad":               "wordpad.exe",
    "configurações":         "ms-settings:",
    "loja":                  "ms-windows-store:",
    "fotos":                 "ms-photos:",
    "relógio":               "ms-clock:",
}

# Aliases: o que o usuário diz → nome real do atalho (parcial)
_APP_ALIASES: dict[str, str] = {
    "vs code":           "visual studio code",
    "vscode":            "visual studio code",
    "obs":               "obs studio",
    "vlc":               "vlc media player",
    "chrome":            "google chrome",
    "photoshop":         "adobe photoshop",
    "illustrator":       "adobe illustrator",
    "after effects":     "adobe after effects",
    "premiere":          "adobe premiere",
    "capcut":            "capcut",
    "figma":             "figma",
    "cursor":            "cursor",
    "git":               "git bash",
    "excel":             "microsoft excel",
    "word":              "microsoft word",
    "powerpoint":        "microsoft powerpoint",
    "teams":             "microsoft teams",
    "outlook":           "microsoft outlook",
}

def _buscar_atalho(nome: str) -> str | None:
    """Procura atalho .lnk em todas as pastas relevantes (Menu Iniciar + Desktop)."""
    nome_l = _APP_ALIASES.get(nome.lower().strip(), nome.lower().strip())

    pastas = [
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu"),
        r"C:\ProgramData\Microsoft\Windows\Start Menu",
        os.path.expandvars(r"%USERPROFILE%\Desktop"),
        os.path.expandvars(r"%PUBLIC%\Desktop"),
    ]
    melhor: tuple[float, str] | None = None
    for pasta in pastas:
        if not os.path.exists(pasta):
            continue
        for lnk in glob.glob(os.path.join(pasta, "**", "*.lnk"), recursive=True):
            base = os.path.splitext(os.path.basename(lnk))[0].lower()
            if nome_l == base:
                return lnk                                    # match exato
            if nome_l in base:
                score = len(nome_l) / max(len(base), 1)
                if melhor is None or score > melhor[0]:
                    melhor = (score, lnk)
            elif base in nome_l and len(base) > 3:
                score = len(base) / max(len(nome_l), 1)
                if melhor is None or score > melhor[0]:
                    melhor = (score, lnk)
    return melhor[1] if (melhor and melhor[0] >= 0.20) else None

def _buscar_no_registro(nome: str) -> str | None:
    """Procura o InstallLocation do app no Registro e devolve o exe principal."""
    nome_l = nome.lower()
    chaves = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER,  r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, caminho in chaves:
        try:
            with winreg.OpenKey(hive, caminho) as raiz:
                for i in range(winreg.QueryInfoKey(raiz)[0]):
                    try:
                        with winreg.OpenKey(raiz, winreg.EnumKey(raiz, i)) as sub:
                            try:
                                disp = winreg.QueryValueEx(sub, "DisplayName")[0].lower()
                                if nome_l in disp:
                                    loc = winreg.QueryValueEx(sub, "InstallLocation")[0]
                                    if loc and os.path.isdir(loc):
                                        # procura o exe com nome mais parecido
                                        for exe in glob.glob(os.path.join(loc, "*.exe")):
                                            return exe
                            except OSError:
                                pass
                    except OSError:
                        pass
        except OSError:
            pass
    return None

def abrir_app(nome: str) -> bool:
    """
    Abre um aplicativo usando múltiplas estratégias:
    1. Dict de apps do sistema (instant)
    2. Atalhos no Menu Iniciar (.lnk)
    3. Registro do Windows (InstallLocation)
    4. comando 'where' (PATH)
    Retorna True se abriu, False se não encontrou.
    """
    n = nome.lower().strip()

    # 1. Apps sistema
    for k, cmd in _APPS_SISTEMA.items():
        if k in n or n in k:
            if cmd.startswith("ms-"):
                subprocess.Popen(f"start {cmd}", shell=True)
            else:
                subprocess.Popen(cmd)
            return True

    # 2. Menu Iniciar
    lnk = _buscar_atalho(n)
    if lnk:
        _log(f"[APP] Atalho encontrado: {lnk}")
        os.startfile(lnk)
        return True

    # 3. Registro
    exe = _buscar_no_registro(n)
    if exe:
        _log(f"[APP] Registro: {exe}")
        subprocess.Popen(exe)
        return True

    # 4. PATH
    try:
        result = subprocess.run(["where", nome], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            exe = result.stdout.strip().splitlines()[0]
            subprocess.Popen(exe)
            return True
    except Exception:
        pass

    return False

# ─── AÇÕES DO PC (funções de nível de módulo — reaproveitadas pelo matcher
# exato e pelo classificador de IA) ────────────────────────────────────────────
def media_key(char_code: int) -> None:
    subprocess.Popen(
        ["powershell", "-c",
         f"(New-Object -ComObject WScript.Shell).SendKeys([char]{char_code})"],
        creationflags=subprocess.CREATE_NO_WINDOW
    )

def vol_key(code: int, times: int = 1) -> None:
    for _ in range(times):
        subprocess.Popen(
            ["powershell", "-c",
             f"(New-Object -ComObject WScript.Shell).SendKeys([char]{code})"],
            creationflags=subprocess.CREATE_NO_WINDOW
        )

def fechar_app(nome: str) -> bool:
    """Fecha o(s) processo(s) cujo nome bate (parcial, case-insensitive)."""
    try:
        result = subprocess.run(
            ["powershell", "-c",
             f"Get-Process | Where-Object {{ $_.ProcessName -like '*{nome}*' }} "
             f"| Stop-Process -Force -ErrorAction SilentlyContinue; "
             f"Write-Output $?"],
            capture_output=True, text=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return "True" in result.stdout
    except Exception as e:
        _log(f"[FECHAR_APP] Erro: {e}")
        return False

def ler_clipboard() -> str:
    try:
        result = subprocess.run(
            ["powershell", "-c", "Get-Clipboard"],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.stdout.strip()
    except Exception:
        return ""

def escrever_clipboard(texto: str) -> bool:
    try:
        subprocess.run(
            ["powershell", "-c", "Set-Clipboard", "-Value", texto],
            capture_output=True, text=True, timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return True
    except Exception:
        return False

def minimizar_tudo() -> None:
    subprocess.Popen(
        ["powershell", "-c", "(New-Object -ComObject Shell.Application).MinimizeAll()"],
        creationflags=subprocess.CREATE_NO_WINDOW
    )

# ─── WiZ LOCAL (UDP direto na rede — sem cloud, sem reconexão) ───────────────
import socket as _socket_mod

_WIZ_PORT = 38899
_wiz_map: dict[str, str] = {}   # nome_normalizado → ip

def _normalize(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFD", s.lower()).encode("ascii", "ignore").decode().strip()

def _wiz_udp(ip: str, payload: dict, timeout: float = 0.7) -> dict | None:
    try:
        sock = _socket_mod.socket(_socket_mod.AF_INET, _socket_mod.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(json.dumps(payload).encode(), (ip, _WIZ_PORT))
        data, _ = sock.recvfrom(512)
        return json.loads(data.decode())
    except Exception:
        return None
    finally:
        try: sock.close()
        except Exception: pass

_WIZ_CACHE_FILE = os.path.join(_BASE_DIR, "wiz_cache.json")
_WIZ_CACHE_TTL  = 86_400  # 24 horas


def _wiz_get_local_subnet() -> str | None:
    """Retorna o prefixo da subnet local, ex: '192.168.1'."""
    try:
        s = _socket_mod.socket(_socket_mod.AF_INET, _socket_mod.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        parts = ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:3])
    except Exception:
        pass
    return None


def _wiz_probe_ip(ip: str, results: dict, lock: threading.Lock) -> None:
    """Sonda um único IP: se responder ao getPilot, registra MAC→IP."""
    resp = _wiz_udp(ip, {"method": "getPilot", "params": {}}, timeout=0.35)
    if resp:
        mac = (resp.get("result") or {}).get("mac", "").lower()
        if mac:
            with lock:
                results[mac] = ip


def _wiz_scan_subnet(subnet: str) -> dict[str, str]:
    """Scan paralelo da subnet (ex: '192.168.1') → {mac: ip}."""
    results: dict[str, str] = {}
    lock = threading.Lock()
    threads = []
    for i in range(1, 255):
        ip = f"{subnet}.{i}"
        t = threading.Thread(target=_wiz_probe_ip, args=(ip, results, lock), daemon=True)
        threads.append(t)
    # Máximo 80 threads simultâneas para não sobrecarregar
    batch_size = 80
    for start in range(0, len(threads), batch_size):
        batch = threads[start:start + batch_size]
        for t in batch:
            t.start()
        for t in batch:
            t.join(timeout=1.0)
    return results


def _wiz_fetch_names_from_argos() -> dict[str, str]:
    """Chama Argos /api/ha?action=wiz-devices → {mac_lower: nome}."""
    if not argos_ha_url or not argos_ha_key or not requests:
        return {}
    try:
        r = requests.get(
            f"{argos_ha_url}?action=wiz-devices",
            headers={"x-ha-key": argos_ha_key},
            timeout=12,
        )
        if r.status_code == 200:
            data = r.json()
            return {d["mac"].lower(): d["name"] for d in data.get("devices", [])}
    except Exception as e:
        _log(f"[WIZ] Erro ao buscar nomes no Argos: {e}")
    return {}


def _wiz_discover() -> None:
    """Descobre lâmpadas WiZ: cache → scan de subnet + nomes do Argos."""
    global _wiz_map

    # 1. Tenta carregar do cache em disco (válido por 24 h)
    # Cache guarda mac→ip; nomes são reaplicados do config a cada startup
    # para que mudanças no wiz_devices sejam refletidas sem apagar o cache.
    try:
        if os.path.exists(_WIZ_CACHE_FILE):
            cached = json.loads(open(_WIZ_CACHE_FILE).read())
            if time.time() - cached.get("ts", 0) < _WIZ_CACHE_TTL:
                cached_mac_ip = cached.get("mac_ip", {})
                if cached_mac_ip:
                    first_ip = next(iter(cached_mac_ip.values()))
                    if _wiz_udp(first_ip, {"method": "getPilot", "params": {}}, timeout=0.5):
                        config_names = {k.lower(): _normalize(v) for k, v in _wiz_devices_config.items()}
                        argos_names  = _wiz_fetch_names_from_argos()
                        rebuilt: dict[str, str] = {}
                        unnamed: list[str] = []
                        for mac, ip in cached_mac_ip.items():
                            name = config_names.get(mac) or _normalize(argos_names.get(mac, ""))
                            if name:
                                rebuilt[name] = ip
                            else:
                                rebuilt[mac] = ip
                                unnamed.append(mac)
                        _wiz_map = rebuilt
                        _log(f"[WIZ] {len(rebuilt)} lâmpada(s) via cache: {list(rebuilt.keys())}")
                        if unnamed:
                            _log(f"[WIZ] MACs sem nome (adicione em config.py wiz_devices): {unnamed}")
                        return
    except Exception:
        pass

    # 2. Scan de subnet (unicast paralelo — funciona mesmo com firewall bloqueando broadcast)
    subnet = _wiz_get_local_subnet()
    if not subnet:
        _log("[WIZ] Não foi possível determinar subnet local.")
        return

    _log(f"[WIZ] Escaneando subnet {subnet}.0/24...")
    mac_to_ip = _wiz_scan_subnet(subnet)

    if not mac_to_ip:
        _log("[WIZ] Nenhuma lâmpada WiZ encontrada na rede.")
        return

    # 3. Combina nomes: config.py → Argos cloud → MAC como fallback
    config_names = {k.lower(): _normalize(v) for k, v in _wiz_devices_config.items()}
    argos_names  = _wiz_fetch_names_from_argos()  # vazio se conta WiZ não vinculada

    new_map: dict[str, str] = {}
    unnamed: list[str] = []
    for mac, ip in mac_to_ip.items():
        name = config_names.get(mac) or _normalize(argos_names.get(mac, ""))
        if name:
            new_map[name] = ip
        else:
            new_map[mac] = ip  # usa MAC como chave temporária
            unnamed.append(mac)

    _wiz_map = new_map
    _log(f"[WIZ] {len(new_map)} lâmpada(s) encontrada(s): {list(new_map.keys())}")
    if unnamed:
        _log(f"[WIZ] MACs sem nome (adicione em config.py wiz_devices): {unnamed}")

    # 4. Salva cache em disco (mac→ip, não nome→ip, para suportar mudanças no config)
    try:
        with open(_WIZ_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "mac_ip": mac_to_ip}, f)
    except Exception:
        pass

def wiz_local_control(query: str, state: bool | None = None, brightness: int | None = None) -> bool:
    """Controla lâmpada WiZ local via UDP. Retorna True se ao menos uma respondeu."""
    if not _wiz_map:
        return False
    q = _normalize(query)
    matched = [ip for key, ip in _wiz_map.items() if q in key or key in q]
    if not matched:
        # Se só tem uma lâmpada na rede, usa ela (caso comum)
        if len(_wiz_map) == 1:
            matched = list(_wiz_map.values())
        else:
            return False
    params: dict = {}
    if state is not None:
        params["state"] = state
    if brightness is not None:
        params["state"] = True
        params["dimming"] = max(10, min(100, brightness))
    if not params:
        return False
    ok = False
    for ip in matched:
        resp = _wiz_udp(ip, {"method": "setPilot", "params": params})
        if resp and resp.get("result", {}).get("success"):
            ok = True
    return ok

# ─── EWELINK (tomadas/dispositivos inteligentes) ─────────────────────────────
try:
    import requests
except ImportError:
    requests = None

_ew_token   : str | None = None
_ew_devices : dict       = {}   # {deviceid: {"name": str, "state": "on"|"off"}}

def _ew_init() -> bool:
    global _ew_token, _ew_devices
    if not ewelink_email or not ewelink_password or not requests:
        return False
    try:
        body = {
            "email": ewelink_email,
            "password": ewelink_password,
            "devToken": "",
            "userAgent": "Mozilla/5.0",
            "platform": "web",
            "version": 8,
        }
        r = requests.post(
            f"https://{ewelink_region}-api.coolkit.cc/v2/user/login",
            json=body, timeout=10
        )
        data = r.json()
        if data.get("error") == 0:
            _ew_token = data["data"]["at"]
            _log("[EWELINK] Login OK")
            return _ew_load_devices()
        _log(f"[EWELINK] Login erro: {data.get('msg', data)}")
    except Exception as e:
        _log(f"[EWELINK] Erro login: {e}")
    return False

def _ew_load_devices() -> bool:
    global _ew_devices
    if not _ew_token or not requests:
        return False
    try:
        r = requests.get(
            f"https://{ewelink_region}-api.coolkit.cc/v2/device/thing",
            headers={"Authorization": f"Bearer {_ew_token}"},
            timeout=10
        )
        data = r.json()
        if data.get("error") == 0:
            for item in data["data"].get("thingList", []):
                d = item.get("itemData", {})
                did = d.get("deviceid", "")
                _ew_devices[did] = {
                    "name": d.get("name", did).lower(),
                    "state": d.get("params", {}).get("switch", "off"),
                }
            _log(f"[EWELINK] {len(_ew_devices)} dispositivos")
            return True
    except Exception as e:
        _log(f"[EWELINK] Erro get_devices: {e}")
    return False

def _ew_find(query: str) -> str | None:
    q = query.lower().strip()
    for did, info in _ew_devices.items():
        if q in info["name"] or info["name"] in q:
            return did
    return next(iter(_ew_devices), None)

def ew_control(query: str, state: bool) -> None:
    global _ew_token
    if not requests:
        say("Modulo requests nao disponivel, senhor.")
        return
    if not _ew_token:
        if not _ew_init():
            return

    did = _ew_find(query)
    if not did:
        say("Dispositivo nao encontrado, senhor.")
        return

    nome = _ew_devices[did]["name"] or "tomada"
    say_bg(f"{'Ligando' if state else 'Desligando'} {nome}, senhor.")

    try:
        r = requests.post(
            f"https://{ewelink_region}-api.coolkit.cc/v2/device/thing/status",
            json={"deviceid": did, "state": 1 if state else 0},
            headers={"Authorization": f"Bearer {_ew_token}"},
            timeout=10
        )
        if r.json().get("error") == 0:
            _ew_devices[did]["state"] = "on" if state else "off"
        else:
            say(f"Erro ao controlar {nome}, senhor.")
    except Exception as e:
        _log(f"[EWELINK] Erro switch: {e}")
        say(f"Erro de conexao com {nome}, senhor.")

def ew_init_bg() -> None:
    if ewelink_email and ewelink_password:
        _ew_init()

# ─── ARGOS (ponte de casa inteligente — Hue, eWeLink, Tuya, Xiaomi) ──────────
# Todo o app Argos (celular) já controla essas marcas. Em vez de reimplementar
# cada uma aqui, o Argos (PC) manda o comando de voz cru pro backend do Argos, que
# identifica o dispositivo certo (com IA quando o atalho rápido não resolve) e
# executa a ação — cobrindo tudo que o Argos já sabe fazer, não só eWeLink.
_DEVICE_TRIGGER_WORDS = (
    "ligar", "liga", "desligar", "desliga", "acender", "acende", "apagar", "apaga",
    "ativar", "ativa", "desativar", "desativa", "turn on", "turn off",
    "ventilador", "luzes", "luz", "lâmpada", "lampada", "tomada", "tomadas",
    "força", "forca", "velocidade", "oscila", "oscilar", "oscilação", "oscilacao",
    "ângulo", "angulo", "brilho", "modo natural", "modo direto",
)
_DEVICE_EXCLUDE_WORDS = (
    "computador", " pc", "jarvis", "computer", "música", "musica",
    "som ", "volume", "reprodução", "reproducao", "tela",
)

def _looks_like_device_command(q: str) -> bool:
    if any(x in q for x in _DEVICE_EXCLUDE_WORDS):
        return False
    return any(w in q for w in _DEVICE_TRIGGER_WORDS)

def argos_command(query: str) -> str | None:
    """Encaminha o comando pro backend do Argos. Devolve o texto falado, ou None se falhou."""
    if not requests or not argos_ha_key:
        return None
    try:
        r = requests.post(
            argos_ha_url,
            json={"message": query},
            headers={"x-ha-key": argos_ha_key},
            timeout=12,
        )
        if r.status_code != 200:
            _log(f"[ARGOS] HTTP {r.status_code}: {r.text[:200]}")
            return None
        reply = r.json().get("reply")
        if not reply:
            return None
        say(reply)
        return reply
    except Exception as e:
        _log(f"[ARGOS] Erro de conexão: {e}")
        return None

_MAX_FOLLOWUP_DEPTH = 2

def _dispatch(command: str, depth: int = 0) -> None:
    """Processa um comando já sem wake word."""
    if not command:
        return
    # process_command cuida de dispositivos (WiZ/Argos) e ações do PC.
    # Se retornou False, é conversa → vai direto pro Groq local (rápido).
    if process_command(command):
        return
    reply = chat(command)
    _maybe_listen_followup(reply, depth)

def _maybe_listen_followup(reply_text: str | None, depth: int = 0) -> None:
    """Se a última fala foi uma pergunta, ouve de novo por alguns segundos sem
    exigir o wake word — se não vier nada, volta a esperar 'Argos' normalmente."""
    if depth >= _MAX_FOLLOWUP_DEPTH or not reply_text:
        return
    if not reply_text.rstrip().rstrip('"”').endswith('?'):
        return
    _log("[FOLLOWUP] Pergunta feita — ouvindo a resposta sem precisar do wake word...")
    follow_up = take_command(max_seconds=5.0).strip()
    if not follow_up:
        _log("[FOLLOWUP] Nada dito — voltando a esperar o wake word.")
        return
    _log(f"[FOLLOWUP-COMANDO] '{follow_up}'")
    _dispatch(follow_up, depth + 1)

SITES = {
    "youtube":   "https://www.youtube.com",
    "wikipedia": "https://www.wikipedia.org",
    "google":    "https://www.google.com",
    "github":    "https://www.github.com",
    "spotify":   "https://open.spotify.com",
    "netflix":   "https://www.netflix.com",
    "twitter":   "https://www.twitter.com",
    "instagram": "https://www.instagram.com",
    "reddit":    "https://www.reddit.com",
    "amazon":    "https://www.amazon.com",
    "gmail":     "https://mail.google.com",
    "chatgpt":   "https://chat.openai.com",
    "claude":    "https://claude.ai",
    "maps":      "https://maps.google.com",
    "mapas":     "https://maps.google.com",
    "notícias":  "https://news.google.com",
    "clima":     "https://weather.com",
    "tradutor":  "https://translate.google.com",
}

# ─── PROCESSADOR DE COMANDOS ──────────────────────────────────────────────────
_PAPERCLIP_TRIGGER_PHRASES = (
    "paperclip", "relatório", "relatorio", "meus projetos", "meus agentes", "meu agente",
    "resumo do argos", "resumo do projeto", "o que o argos fez",
    "quem está trabalhando", "quem esta trabalhando",
    "tarefas pendentes", "algo bloqueado", "está bloqueado", "esta bloqueado",
    "tarefas falharam", "algum agente", "precisa de decisão", "precisa de decisao",
    "o que mudou desde", "o que foi feito hoje", "o que foi feito ontem",
    "como estão meus", "como estao meus",
)


def _looks_like_paperclip_query(q: str) -> bool:
    return any(p in q for p in _PAPERCLIP_TRIGGER_PHRASES)


# New voice_facade intents (#11) - deliberately distinct phrases from
# _PAPERCLIP_TRIGGER_PHRASES above so the two never shadow each other.
_ORCHESTRATOR_STATUS_PHRASES = (
    "status da orquestração", "status da orquestracao",
    "status do orchestrator", "como está a orquestração", "como esta a orquestracao",
)
_ORCHESTRATOR_REPORT_PHRASES = (
    "relatório da orquestração", "relatorio da orquestracao",
    "relatório do orchestrator", "relatorio do orchestrator",
)
_ORCHESTRATOR_CONTROL_PHRASES = (
    "pausar orquestração", "pausar orquestracao",
    "retomar orquestração", "retomar orquestracao",
    "controle da orquestração", "controle da orquestracao",
)


def process_command(query: str) -> bool:
    q = query.lower().strip()

    # ── Paperclip (status dos agentes/projetos) — checa antes do resto pra não
    # cair em nenhum matcher genérico de "projeto"/"agente" ────────────────────
    if _looks_like_paperclip_query(q):
        paperclip_report(query)
        return True

    # ── Orquestrador (status/relatório/controle) — facade em construção
    # (#11): qualquer exceção do orchestrator nunca derruba o Jarvis, mesmo
    # padrão já usado com paperclip_client.py ──────────────────────────────
    if any(p in q for p in _ORCHESTRATOR_STATUS_PHRASES):
        try:
            reply = voice_facade.handle_status_query(query)
        except Exception as e:
            _log(f"[ORCHESTRATOR] Erro no facade (status): {e}")
            reply = None
        if reply:
            say(reply)
            return True

    if any(p in q for p in _ORCHESTRATOR_REPORT_PHRASES):
        try:
            reply = voice_facade.handle_report_query()
        except Exception as e:
            _log(f"[ORCHESTRATOR] Erro no facade (relatório): {e}")
            reply = None
        if reply:
            say(reply)
            return True

    if any(p in q for p in _ORCHESTRATOR_CONTROL_PHRASES):
        try:
            reply = voice_facade.handle_control_query(query)
        except Exception as e:
            _log(f"[ORCHESTRATOR] Erro no facade (controle): {e}")
            reply = None
        if reply:
            say(reply)
            return True

    # ── Reiniciar Argos ──────────────────────────────────────────────────────
    if any(p in q for p in ["reiniciar jarvis", "jarvis reiniciar", "restart jarvis",
                             "reinicia o jarvis", "reinicia jarvis"]):
        say("Reiniciando, senhor. Volto em instantes.")
        pyw = sys.executable.replace("python.exe", "pythonw.exe")
        if not os.path.exists(pyw):
            pyw = sys.executable
        subprocess.Popen(
            [pyw, os.path.abspath(__file__)],
            cwd=_BASE_DIR,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        )
        sys.exit(0)

    # ── Encerrar ────────────────────────────────────────────────────────────
    if any(p in q for p in ["jarvis encerrar", "tchau jarvis", "desligar jarvis",
                             "fechar jarvis", "jarvis desligar", "jarvis off",
                             "jarvis quit", "goodbye jarvis"]):
        say("Até logo, senhor. Argos encerrando.")
        sys.exit(0)

    # ── Resetar conversa ────────────────────────────────────────────────────
    if any(p in q for p in ["resetar conversa", "limpar memória", "esquecer tudo",
                             "nova conversa", "reset chat", "apagar histórico"]):
        global chatHistory
        chatHistory = []
        say("Memória da conversa apagada, senhor.")
        return True

    # ── Hora ────────────────────────────────────────────────────────────────
    if any(p in q for p in ["que horas", "que hora", "horas são", "hora atual", "what time"]):
        hora = datetime.datetime.now().strftime("%H:%M")
        say(f"São {hora}, senhor.")
        return True

    # ── Data ────────────────────────────────────────────────────────────────
    if any(p in q for p in ["que dia", "qual a data", "data de hoje", "dia de hoje",
                             "que dia é hoje", "what date", "today"]):
        meses = ["janeiro","fevereiro","março","abril","maio","junho",
                 "julho","agosto","setembro","outubro","novembro","dezembro"]
        dias  = ["segunda-feira","terça-feira","quarta-feira","quinta-feira",
                 "sexta-feira","sábado","domingo"]
        now   = datetime.datetime.now()
        dia_semana = dias[now.weekday()]
        say(f"Hoje é {dia_semana}, {now.day} de {meses[now.month-1]} de {now.year}, senhor.")
        return True

    # ── Sites ────────────────────────────────────────────────────────────────
    for name, url in SITES.items():
        if f"abrir {name}" in q or f"abre {name}" in q or f"open {name}" in q:
            say_bg(f"Abrindo {name}, senhor.")
            webbrowser.open(url)
            return True

    # ── Busca ────────────────────────────────────────────────────────────────
    if "youtube" in q and any(w in q for w in ["pesquisar", "buscar", "procurar", "tocar", "reproduzir", "search", "play"]):
        term = re.sub(r"(pesquisar|buscar|procurar|tocar|reproduzir|search|play).*?(no |no youtube|youtube)", "", q).strip()
        term = re.sub(r"(youtube|no youtube)", "", term).strip()
        if term:
            webbrowser.open(f"https://www.youtube.com/results?search_query={term.replace(' ', '+')}")
            say_bg(f"Pesquisando {term} no YouTube, senhor.")
            return True

    if any(p in q for p in ["pesquisar no google", "buscar no google", "google pesquisa",
                             "procurar no google", "search google"]):
        term = re.sub(r"(pesquisar|buscar|procurar|search).*?(no google|google)", "", q).strip()
        if term:
            webbrowser.open(f"https://www.google.com/search?q={term.replace(' ', '+')}")
            say_bg(f"Pesquisando {term} no Google, senhor.")
            return True

    if "wikipedia" in q and any(w in q for w in ["pesquisar", "buscar", "procurar", "search"]):
        term = re.sub(r"(pesquisar|buscar|procurar|search).*?(na |no |wikipedia)", "", q).strip()
        term = re.sub(r"wikipedia", "", term).strip()
        if term:
            webbrowser.open(f"https://pt.wikipedia.org/wiki/{term.replace(' ', '_')}")
            say_bg(f"Abrindo Wikipedia para {term}, senhor.")
            return True

    # ── Aplicativos (busca dinâmica no Menu Iniciar + Registro) ─────────────
    if any(p in q for p in ["abrir ", "abre ", "open ", "iniciar ", "inicia ", "lançar "]):
        app_part = re.sub(r"^(abrir|abre|open|iniciar|inicia|lançar)\s+", "", q).strip()
        if app_part:
            if abrir_app(app_part):
                say_bg(f"Abrindo {app_part}, senhor.")
            else:
                say(f"Não encontrei o aplicativo {app_part} instalado, senhor.")
            return True

    # ── Fechar aplicativo ────────────────────────────────────────────────────
    if any(p in q for p in ["fechar ", "feche ", "close "]) and not _WAKE_WORD_RE.search(q):
        app_part = re.sub(r"^(fechar|feche|close)\s+", "", q).strip()
        if app_part:
            if fechar_app(app_part):
                say_bg(f"Fechando {app_part}, senhor.")
            else:
                say(f"Não encontrei {app_part} em execução, senhor.")
            return True

    # ── Minimizar tudo ───────────────────────────────────────────────────────
    if any(p in q for p in ["minimizar tudo", "minimizar tudo o", "mostrar área de trabalho",
                             "mostrar area de trabalho", "show desktop"]):
        minimizar_tudo()
        say_bg("Feito, senhor.")
        return True

    # ── Casa inteligente — WiZ local primeiro, depois Argos ─────────────────────
    if _looks_like_device_command(q):
        # Tenta WiZ local (UDP direto, sem cloud, ~5ms)
        _ON  = ["liga", "ligar", "acende", "acender", "ativar", "ativa", "ligado"]
        _OFF = ["desliga", "desligar", "apaga", "apagar", "desativar", "desativa"]
        _is_on  = any(w in q.split() for w in _ON)
        _is_off = any(w in q.split() for w in _OFF)
        _LIGHT_WORDS = ["luz", "luzes", "lampada", "lampadas", "lampada", "light"]
        if _wiz_map and (_is_on or _is_off) and any(w in q for w in _LIGHT_WORDS):
            _obj = re.sub(r'\b(liga|ligar|acende|acender|ativar|ativa|desliga|desligar|apaga|apagar|desativar|desativa)\b', '', q).strip()
            if wiz_local_control(_obj, state=True if _is_on else False):
                say_bg(f"{'Ligando' if _is_on else 'Apagando'} a luz, senhor.")
                return True

        # Fallback: Argos (cloud — cobre ventilador, tomadas, outras marcas)
        reply = argos_command(query)
        if reply is not None:
            _maybe_listen_followup(reply)
            return True
        # Argos indisponível — sem fallback de dispositivo
        say_bg("Sem conexão com Argos, senhor.")
        return True

    # ── Controle de mídia ────────────────────────────────────────────────────
    if any(p in q for p in ["pausar música", "pausar", "pause", "play pause",
                             "reproduzir", "tocar música", "continuar música"]):
        media_key(179)
        say_bg("Feito, senhor.")
        return True

    if any(p in q for p in ["próxima música", "próxima faixa", "avançar música",
                             "next", "pular música"]):
        media_key(176)
        say_bg("Próxima faixa, senhor.")
        return True

    if any(p in q for p in ["música anterior", "faixa anterior", "voltar música",
                             "previous", "voltar faixa"]):
        media_key(177)
        say_bg("Faixa anterior, senhor.")
        return True

    if any(p in q for p in ["parar música", "stop música", "parar reprodução"]):
        media_key(178)
        say_bg("Reprodução pausada, senhor.")
        return True

    # ── Captura de tela ──────────────────────────────────────────────────────
    if any(p in q for p in ["captura de tela", "screenshot", "tirar foto da tela", "printscreen"]):
        tirar_screenshot()
        return True

    # ── Timer ────────────────────────────────────────────────────────────────
    m = re.search(r"(?:timer|cronômetro|alarme) (?:de |por )?(\d+) (segundo|minuto|hora)s?", q)
    if m:
        val, unit = int(m.group(1)), m.group(2)
        secs = val * (3600 if unit == "hora" else 60 if unit == "minuto" else 1)
        definir_timer(secs)
        return True

    # ── Volume ───────────────────────────────────────────────────────────────
    if any(p in q for p in ["silenciar", "mutar", "sem som", "mute"]):
        vol_key(173)
        say_bg("Silenciado, senhor.")
        return True

    if any(p in q for p in ["dessilenciar", "desmutar", "ligar som", "unmute"]):
        vol_key(173)
        say_bg("Som ativado, senhor.")
        return True

    if any(p in q for p in ["aumentar volume", "volume alto", "mais alto", "volume up", "louder"]):
        vol_key(175, 5)
        say_bg("Volume aumentado, senhor.")
        return True

    if any(p in q for p in ["diminuir volume", "volume baixo", "mais baixo", "volume down", "quieter"]):
        vol_key(174, 5)
        say_bg("Volume diminuído, senhor.")
        return True

    # ── Controle do sistema ──────────────────────────────────────────────────
    if any(p in q for p in ["desligar computador", "desligar o pc", "shutdown computer"]):
        say_bg("Desligando o sistema em 5 segundos, senhor.")
        os.system("shutdown /s /t 5")
        return True

    if any(p in q for p in ["reiniciar computador", "reiniciar o pc", "restart computer"]):
        say_bg("Reiniciando o sistema, senhor.")
        os.system("shutdown /r /t 5")
        return True

    if any(p in q for p in ["cancelar desligamento", "cancelar shutdown", "abort shutdown"]):
        os.system("shutdown /a")
        say_bg("Desligamento cancelado, senhor.")
        return True

    if "bloquear" in q and any(w in q for w in ["tela", "computador", "pc", "screen"]):
        say_bg("Bloqueando a tela, senhor.")
        ctypes.windll.user32.LockWorkStation()
        return True

    # ── Endereço IP ──────────────────────────────────────────────────────────
    if any(p in q for p in ["endereço ip", "meu ip", "qual meu ip", "ip address"]):
        import socket
        try:
            ip = socket.gethostbyname(socket.gethostname())
            say(f"Seu endereço IP local é {ip}, senhor.")
        except Exception:
            say("Não consegui determinar seu endereço IP, senhor.")
        return True

    # ── Bateria ──────────────────────────────────────────────────────────────
    if any(p in q for p in ["bateria", "nível de bateria", "battery"]):
        try:
            result = subprocess.run(
                ["powershell", "-c", "(Get-WmiObject -Class Win32_Battery).EstimatedChargeRemaining"],
                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW
            )
            level = result.stdout.strip()
            if level:
                say(f"A bateria está em {level} por cento, senhor.")
            else:
                say("Nenhuma bateria detectada. Você está na tomada, senhor.")
        except Exception:
            say("Não consegui verificar a bateria, senhor.")
        return True

    # ── Área de transferência ────────────────────────────────────────────────
    if any(p in q for p in ["o que tem na área de transferência", "ler clipboard", "o que está copiado"]):
        content = ler_clipboard()
        if content:
            say(f"Sua área de transferência contém: {content[:200]}")
        else:
            say("A área de transferência está vazia, senhor.")
        return True

    # ── Ação no PC via IA — fallback pra frases que não bateram exato acima ──
    # Cobre "abre o spotify pra mim", "fecha o navegador", "copia isso aqui" etc,
    # em vez de exigir a frase exata dos matchers de cima.
    if ai_pc_action(query):
        return True

    return False

# ─── WATCHDOG (auto-reinício se o loop principal travar) ─────────────────────
_last_heartbeat = time.time()
_WATCHDOG_TIMEOUT = 180  # segundos sem heartbeat → reinicia

def _watchdog_loop() -> None:
    while True:
        time.sleep(30)
        if time.time() - _last_heartbeat > _WATCHDOG_TIMEOUT:
            _log("[WATCHDOG] Loop travado há mais de 3 min — reiniciando...")
            pyw = sys.executable.replace("python.exe", "pythonw.exe")
            if not os.path.exists(pyw):
                pyw = sys.executable
            subprocess.Popen(
                [pyw, os.path.abspath(__file__)],
                cwd=_BASE_DIR,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            )
            sys.exit(0)

# ─── PRINCIPAL ────────────────────────────────────────────────────────────────
def _log(msg: str) -> None:
    """Print seguro — redireciona para arquivo quando rodando oculto (pythonw)."""
    try:
        print(msg)
    except Exception:
        pass

if __name__ == "__main__":
    # Roda oculto e sem ninguém olhando — se algum .exe filho (piper, etc.) crashar
    # por DLL faltando, o Windows normalmente abre um popup "erro do sistema" que
    # fica travado esperando alguém clicar OK. Isso desativa esses popups pra
    # qualquer processo filho: falha rápida e silenciosa em vez de travar.
    try:
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except Exception:
        pass

    # Quando rodando com pythonw.exe, sys.stdout é None.
    # Redireciona para jarvis.log para não travar silenciosamente.
    if sys.stdout is None:
        _log_path = os.path.join(_BASE_DIR, "jarvis.log")
        _log_file = open(_log_path, "w", encoding="utf-8", buffering=1)
        sys.stdout = _log_file
        sys.stderr = _log_file
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    _log("Jarvis (PC) v2.0 - PT-BR")
    _log("=" * 50)
    _log("Diga 'Jarvis' antes de qualquer comando.")
    _log("Exemplos: 'Jarvis que horas sao', 'Jarvis abre o YouTube'")
    _log("=" * 50)

    _write_pid()

    # Watchdog: reinicia automaticamente se o loop principal travar
    threading.Thread(target=_watchdog_loop, daemon=True).start()

    # Baixa Piper + modelo em background (não trava o startup)
    threading.Thread(target=_setup_piper, daemon=True).start()

    # Descobre lâmpadas WiZ na rede local em background
    threading.Thread(target=_wiz_discover, daemon=True).start()

    # Conecta ao eWeLink em background
    threading.Thread(target=ew_init_bg, daemon=True).start()

    calibrate_noise()
    say("Jarvis online.")

    while True:
        _last_heartbeat = time.time()
        _log("-" * 50)
        _log("Aguardando 'Jarvis'...")
        query = take_command()

        if not query:
            continue

        # Só processa se o wake word estiver presente. O Whisper frequentemente
        # transcreve "Argos" como "Argus" — o "o" final átono do português
        # falado soa como "u", e essa é a grafia mais comum pro STT — então
        # aceita as variantes fonéticas mais prováveis, não só a grafia exata.
        if not _WAKE_WORD_RE.search(query):
            _log(f"[IGNORADO] '{query}' (sem wake word)")
            continue

        # Remove o wake word antes de processar o comando
        command = _WAKE_WORD_RE.sub('', query, count=1).strip()

        if not command:
            # Wake word sozinho, sem comando na mesma frase — em vez de já
            # responder "Sim, senhor?" (o que corta o usuário no meio da
            # frase seguinte), sinaliza com um bipe leve e continua ouvindo
            # em silêncio, esperando o comando vir numa frase separada.
            play_listen_chime()
            _log("[WAKE] Wake word sozinho — aguardando o comando...")
            command = take_command(max_seconds=5.0).strip()
            if not command:
                say("Sim, senhor?")
                continue

        _log(f"[COMANDO] '{command}'")
        _dispatch(command)
