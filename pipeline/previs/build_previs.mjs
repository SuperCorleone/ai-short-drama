#!/usr/bin/env node
/**
 * 灰盒预演构建器（AI 短剧 M0）：分镜脚本 -> Godot 灰盒预演帧
 *
 * 流程：
 *   1. 确保 Godot 项目 godot/projects/previs_ep01 存在
 *   2. 用 build_graybox_set 批量 op 建 5 个灰盒 set（每个 set 一次 Godot 进程）
 *   3. 按 shots.json 的镜头规格卡计算相机（景别->距离、视角->机位），
 *      用 render_viewport 的内联相机渲染每镜头 1 帧（不写回场景）
 *   4. 帧移到 pipeline/previs/frames/<shot_id>.png，做像素抽检，
 *      输出 render_report.json 与验收报告 index.md
 *
 * 幂等：set 已存在则跳过（--rebuild 强制重建）；渲染前清理本次涉及镜头的旧帧。
 *
 * 用法：
 *   node pipeline/previs/build_previs.mjs                  # 全量
 *   node pipeline/previs/build_previs.mjs --groups=1,2     # 只跑第 1、2 组
 *   node pipeline/previs/build_previs.mjs --shots=G01-S01  # 只跑指定镜头
 *   node pipeline/previs/build_previs.mjs --skip-sets      # 不重建 set
 *   node pipeline/previs/build_previs.mjs --rebuild        # 强制重建 set
 *   node pipeline/previs/build_previs.mjs --report-only    # 只重生成报告
 */

import { spawn } from 'child_process';
import {
  readFileSync, writeFileSync, existsSync, mkdirSync, rmSync,
  readdirSync, copyFileSync, unlinkSync,
} from 'fs';
import { inflateSync } from 'zlib';
import { join, resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..', '..');
const MCP_DIR = join(REPO_ROOT, 'third_party', 'godot-mcp');
const PROJECT_PATH = join(REPO_ROOT, 'godot', 'projects', 'previs_ep01');
const GODOT_PATH = join(REPO_ROOT, 'godot', 'Godot_v4.7.1-stable_win64.exe');
const SHOTS_JSON = join(REPO_ROOT, 'pipeline', 'previs', 'shots.json');
const FRAMES_DIR = join(REPO_ROOT, 'pipeline', 'previs', 'frames');
const REPORT_JSON = join(REPO_ROOT, 'pipeline', 'previs', 'render_report.json');
const INDEX_MD = join(REPO_ROOT, 'pipeline', 'previs', 'index.md');
const RESOLUTION = { width: 640, height: 360 };
// 四通道导出走 9:16 竖屏（对齐红果/抖音交付比例与 L3 关键帧的 768×1344）
const CHANNELS_DIR = join(REPO_ROOT, 'pipeline', 'previs', 'channels');
const CHANNELS_REPORT = join(REPO_ROOT, 'pipeline', 'previs', 'channels_report.json');
const CHANNEL_RESOLUTION = { width: 576, height: 1024 };

const args = process.argv.slice(2);
const optGroups = args.find((a) => a.startsWith('--groups='))?.split('=')[1]
  ?.split(',').map((s) => parseInt(s.trim(), 10));
const optShots = args.find((a) => a.startsWith('--shots='))?.split('=')[1]?.split(',');
const optSkipSets = args.includes('--skip-sets');
const optRebuild = args.includes('--rebuild');
const optReportOnly = args.includes('--report-only');
const optChannels = args.includes('--channels');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------------------
// 角色与场景定义
// ---------------------------------------------------------------------------

// 角色注册表：灰盒体量（胶囊/sphere）+ 颜色 + 名牌
const CHARS = {
  '王天薇': { node: 'Char_WangTianwei', h: 1.65, shape: 'capsule', color: [0.92, 0.62, 0.66] },
  '王开宇': { node: 'Char_WangKaiyu', h: 1.95, shape: 'capsule', color: [0.35, 0.52, 0.90] },
  '家用机器人': { node: 'Char_HomeRobot', h: 0.70, shape: 'sphere', color: [0.85, 0.85, 0.88] },
  '料理机器人': { node: 'Char_CookRobot', h: 0.70, shape: 'sphere', color: [0.95, 0.80, 0.40] },
  '丧尸': { node: 'Char_Zombie', h: 1.80, shape: 'capsule', color: [0.22, 0.28, 0.22] },
  '骆驼': { node: 'Char_Luotuo', h: 1.72, shape: 'capsule', color: [0.76, 0.60, 0.42] },
};

const FLOOR_COLOR = [0.45, 0.45, 0.48];
const WALL_COLOR = [0.62, 0.62, 0.66];

function charNodes(name, x, z) {
  const c = CHARS[name];
  const nodes = [];
  if (c.shape === 'sphere') {
    nodes.push({ type: 'sphere', name: c.node, position: { x, y: c.h / 2, z }, size: { x: c.h / 2, y: c.h, z: 0 }, color: c.color });
  } else {
    nodes.push({ type: 'capsule', name: c.node, position: { x, y: c.h / 2, z }, size: { x: 0.25, y: c.h, z: 0 }, color: c.color });
  }
  nodes.push({ type: 'label', name: c.node + '_Label', text: name, position: { x, y: c.h + 0.3, z } });
  return nodes;
}

// 基础三点布光：主平行光 + 顶部 Omni 补光 + 侧逆 Omni 轮廓光
function lights(energy = 1.0, omniEnergy = 0.8) {
  return [
    { type: 'light', name: 'KeyLight', light_type: 'DirectionalLight3D', position: { x: 0, y: 4, z: 0 }, rotation: { x: -50, y: -30, z: 0 }, color: [1.0, 0.96, 0.90], energy, shadow_enabled: true },
    { type: 'light', name: 'FillLight', light_type: 'OmniLight3D', position: { x: 0, y: 2.8, z: 1.5 }, color: [0.9, 0.93, 1.0], energy: omniEnergy, shadow_enabled: false },
    { type: 'light', name: 'RimLight', light_type: 'OmniLight3D', position: { x: -2.5, y: 2.2, z: -2.5 }, color: [0.75, 0.82, 1.0], energy: omniEnergy * 0.6, shadow_enabled: false },
  ];
}

function roomShell(w, d, h = 3) {
  return [
    { type: 'plane', name: 'Floor', position: { x: 0, y: 0, z: 0 }, size: { x: w, y: 0, z: d }, color: FLOOR_COLOR },
    { type: 'box', name: 'WallBack', position: { x: 0, y: h / 2, z: -d / 2 }, size: { x: w, y: h, z: 0.2 }, color: WALL_COLOR },
    { type: 'box', name: 'WallLeft', position: { x: -w / 2, y: h / 2, z: 0 }, size: { x: 0.2, y: h, z: d }, color: WALL_COLOR },
  ];
}

// 每个 set：场景壳 + 标志性道具 + 角色默认站位 + 灯光 + 默认相机
const SETS = {
  '卧室': {
    file: 'sets/bedroom.tscn',
    stations: { '王天薇': [0, 1.2], '家用机器人': [-4, 2.5] },
    nodes: [
      ...roomShell(10, 8),
      { type: 'box', name: 'SleepPod1', position: { x: -2.5, y: 0.5, z: -2.6 }, size: { x: 0.9, y: 1.0, z: 2.1 }, color: [0.95, 0.95, 0.96] },
      { type: 'box', name: 'SleepPod2', position: { x: -0.9, y: 0.5, z: -2.6 }, size: { x: 0.9, y: 1.0, z: 2.1 }, color: [0.95, 0.95, 0.96] },
      { type: 'box', name: 'Sofa', position: { x: 2.6, y: 0.35, z: -3.0 }, size: { x: 1.8, y: 0.7, z: 0.8 }, color: [0.90, 0.88, 0.85] },
      { type: 'box', name: 'Table', position: { x: 2.4, y: 0.38, z: -0.4 }, size: { x: 1.2, y: 0.75, z: 0.6 }, color: [0.55, 0.45, 0.38] },
      { type: 'box', name: 'ChargeBase', position: { x: -4, y: 0.1, z: 2.5 }, size: { x: 0.8, y: 0.2, z: 0.8 }, color: [0.35, 0.35, 0.40] },
      ...charNodes('王天薇', 0, 1.2),
      ...charNodes('家用机器人', -4, 2.5),
      ...lights(1.0, 0.7),
    ],
    camera: { position: { x: 4, y: 2.2, z: 5 }, lookAt: { x: -0.5, y: 1, z: -1 }, fov: 60 },
  },
  '厨房': {
    file: 'sets/kitchen.tscn',
    stations: { '王天薇': [-0.5, 0.5], '料理机器人': [3, 2] },
    nodes: [
      ...roomShell(8, 8),
      { type: 'box', name: 'Fridge', position: { x: -2.6, y: 0.95, z: -3.2 }, size: { x: 0.9, y: 1.9, z: 0.8 }, color: [0.88, 0.90, 0.93] },
      { type: 'box', name: 'Counter', position: { x: 0.8, y: 0.45, z: -3.3 }, size: { x: 2.6, y: 0.9, z: 0.7 }, color: [0.70, 0.70, 0.72] },
      { type: 'box', name: 'WallCabinet', position: { x: 0.8, y: 2.1, z: -3.6 }, size: { x: 2.6, y: 0.8, z: 0.4 }, color: [0.78, 0.76, 0.72] },
      { type: 'box', name: 'ChargeBase', position: { x: 3, y: 0.1, z: 2 }, size: { x: 0.8, y: 0.2, z: 0.8 }, color: [0.35, 0.35, 0.40] },
      ...charNodes('王天薇', -0.5, 0.5),
      ...charNodes('料理机器人', 3, 2),
      ...lights(1.1, 0.8),
    ],
    camera: { position: { x: 3, y: 2.0, z: 4 }, lookAt: { x: 0, y: 1, z: -2 }, fov: 60 },
  },
  '玄关': {
    file: 'sets/entry.tscn',
    stations: { '王天薇': [1.2, -1.6], '王开宇': [-0.6, 0.6] },
    nodes: [
      ...roomShell(6, 7),
      { type: 'box', name: 'Door', position: { x: -1.2, y: 1.1, z: -3.4 }, size: { x: 1.2, y: 2.2, z: 0.15 }, color: [0.45, 0.38, 0.32] },
      { type: 'box', name: 'Mirror', position: { x: 1.8, y: 1.05, z: -3.35 }, size: { x: 0.9, y: 1.9, z: 0.08 }, color: [0.80, 0.88, 0.92] },
      { type: 'box', name: 'ShoeBench', position: { x: -2.2, y: 0.25, z: -2.6 }, size: { x: 1.0, y: 0.5, z: 0.4 }, color: [0.55, 0.45, 0.38] },
      ...charNodes('王天薇', 1.2, -1.6),
      ...charNodes('王开宇', -0.6, 0.6),
      ...lights(0.9, 0.7),
    ],
    camera: { position: { x: 2.2, y: 1.8, z: 2.6 }, lookAt: { x: 0, y: 1.1, z: -2 }, fov: 60 },
  },
  '雪夜基地': {
    file: 'sets/snowgate.tscn',
    stations: { '王天薇': [0.6, 1.5], '骆驼': [-1.2, 1.0], '丧尸': [0, 4.5] },
    nodes: [
      { type: 'plane', name: 'SnowGround', position: { x: 0, y: 0, z: 0 }, size: { x: 24, y: 0, z: 24 }, color: [0.82, 0.86, 0.92] },
      { type: 'box', name: 'MetalGate', position: { x: 0, y: 2.5, z: -6 }, size: { x: 4.5, y: 5.0, z: 0.4 }, color: [0.40, 0.44, 0.50] },
      { type: 'box', name: 'WallL', position: { x: -4.5, y: 2.5, z: -6 }, size: { x: 4.5, y: 5.0, z: 0.4 }, color: [0.34, 0.37, 0.42] },
      { type: 'box', name: 'WallR', position: { x: 4.5, y: 2.5, z: -6 }, size: { x: 4.5, y: 5.0, z: 0.4 }, color: [0.34, 0.37, 0.42] },
      { type: 'box', name: 'LightTower', position: { x: 4, y: 1.5, z: -3.5 }, size: { x: 0.4, y: 3.0, z: 0.4 }, color: [0.30, 0.30, 0.33] },
      ...charNodes('王天薇', 0.6, 1.5),
      ...charNodes('骆驼', -1.2, 1.0),
      ...charNodes('丧尸', 0, 4.5),
      // 雪夜：冷色低主光 + 探照灯 Spot
      { type: 'light', name: 'KeyLight', light_type: 'DirectionalLight3D', position: { x: 0, y: 5, z: 0 }, rotation: { x: -55, y: -20, z: 0 }, color: [0.55, 0.65, 0.85], energy: 0.6, shadow_enabled: true },
      { type: 'light', name: 'SearchLight', light_type: 'SpotLight3D', position: { x: 4, y: 3.2, z: -3.4 }, rotation: { x: -35, y: 25, z: 0 }, color: [1.0, 0.98, 0.9], energy: 3.0, shadow_enabled: true },
      { type: 'light', name: 'FillLight', light_type: 'OmniLight3D', position: { x: 0, y: 2.5, z: 2 }, color: [0.6, 0.68, 0.85], energy: 0.5, shadow_enabled: false },
    ],
    camera: { position: { x: 6, y: 3, z: 6 }, lookAt: { x: 0, y: 1.5, z: -3 }, fov: 60 },
  },
  '浴缸五金店': {
    file: 'sets/bathroom.tscn',
    stations: { '王天薇': [0, 0.3] },
    nodes: [
      ...roomShell(6, 6),
      { type: 'box', name: 'Bathtub', position: { x: 0, y: 0.3, z: 0 }, size: { x: 1.7, y: 0.6, z: 0.8 }, color: [0.94, 0.94, 0.95] },
      { type: 'box', name: 'Shelf1', position: { x: -2.0, y: 1.0, z: -2.6 }, size: { x: 1.2, y: 2.0, z: 0.5 }, color: [0.60, 0.55, 0.45] },
      { type: 'box', name: 'Shelf2', position: { x: 2.0, y: 1.0, z: -2.6 }, size: { x: 1.2, y: 2.0, z: 0.5 }, color: [0.60, 0.55, 0.45] },
      ...charNodes('王天薇', 0, 0.3),
      ...lights(1.0, 0.8),
    ],
    camera: { position: { x: 2.5, y: 1.8, z: 3 }, lookAt: { x: 0, y: 0.6, z: 0 }, fov: 60 },
  },
};

// ---------------------------------------------------------------------------
// 景别 -> 相机距离；视角 -> 机位
// ---------------------------------------------------------------------------
const SIZE_DIST = { '特写': 0.6, '近景': 1.2, '中景': 2.5, '全景': 5.0, '大全景': 10.0 };

// 取景瞄准点高度（角色身高比例）
function aimHeight(shotSize, h) {
  switch (shotSize) {
    case '特写': return h * 0.85; // 头部
    case '近景': return h * 0.72; // 胸部
    default: return h * 0.5;
  }
}

// 计算某镜头的内联相机 {position, lookAt, fov}
function computeCamera(shot, setDef) {
  const size = shot.shot_size || '全景';
  const dist = SIZE_DIST[size] ?? 5.0;
  const perspective = shot.perspective || '客观';
  const chars = (shot.characters || []).filter((c) => setDef.stations[c]);

  // 主体：subject_hint 里点名的角色优先，否则第一个角色
  let subject = null;
  if (shot.subject_hint) {
    subject = chars.find((c) => shot.subject_hint.includes(c)) || null;
  }
  if (!subject && chars.length) subject = chars[0];

  if (!subject) {
    // 空场景镜头：对场景中心取景
    const d = dist + 2;
    return {
      position: { x: d * 0.6, y: 1.6 + d * 0.25, z: d * 0.8 },
      lookAt: { x: 0, y: 1.0, z: -1 },
      fov: 55,
    };
  }

  const [sx, sz] = setDef.stations[subject];
  const h = CHARS[subject].h;
  const ah = aimHeight(size, h);

  // 双人及多人物镜头：取景重心 + 拉远
  if (chars.length >= 2 && perspective === '客观') {
    const cx = chars.reduce((s, c) => s + setDef.stations[c][0], 0) / chars.length;
    const cz = chars.reduce((s, c) => s + setDef.stations[c][1], 0) / chars.length;
    const d = dist * 1.4;
    return {
      position: { x: cx + d * 0.57, y: ah + d * 0.18, z: cz + d * 0.82 },
      lookAt: { x: cx, y: ah, z: cz },
      fov: 55,
    };
  }

  // POV 目标：镜头里或 set 里的另一个角色，否则场景中心
  const other = chars.find((c) => c !== subject)
    || Object.keys(setDef.stations).find((c) => c !== subject);

  switch (perspective) {
    case '主观': {
      // 角色眼位 POV，看向另一个角色或场景中心
      const eye = { x: sx, y: h * 0.9, z: sz };
      if (other) {
        const [ox, oz] = setDef.stations[other];
        return { position: eye, lookAt: { x: ox, y: CHARS[other].h * 0.7, z: oz }, fov: 60 };
      }
      return { position: eye, lookAt: { x: 0, y: 1.0, z: -2 }, fov: 60 };
    }
    case '侧面':
      return {
        position: { x: sx + dist, y: ah + dist * 0.08, z: sz },
        lookAt: { x: sx, y: ah, z: sz },
        fov: 55,
      };
    case '仰拍':
      return {
        position: { x: sx + dist * 0.34, y: Math.max(0.25, ah - dist * 0.45), z: sz + dist * 0.94 },
        lookAt: { x: sx, y: h * 0.9, z: sz },
        fov: 55,
      };
    case '背后': {
      // 过肩：相机在角色身后，视线越过角色看向对方/场景中心
      const bx = sx - dist * 0.30;
      const bz = sz - dist * 0.95;
      if (other) {
        const [ox, oz] = setDef.stations[other];
        return { position: { x: bx, y: h * 0.85, z: bz }, lookAt: { x: ox, y: CHARS[other].h * 0.6, z: oz }, fov: 55 };
      }
      return { position: { x: bx, y: h * 0.85, z: bz }, lookAt: { x: 0, y: 1.0, z: -2 }, fov: 55 };
    }
    case '客观':
    default:
      // 前 3/4 侧
      return {
        position: { x: sx + dist * 0.57, y: ah + dist * 0.15, z: sz + dist * 0.82 },
        lookAt: { x: sx, y: ah, z: sz },
        fov: 55,
      };
  }
}

// ---------------------------------------------------------------------------
// 极简 MCP stdio 客户端（写法照抄 scripts/mcp_smoke_new_tools.mjs）
// ---------------------------------------------------------------------------
const server = spawn(process.execPath, [join(MCP_DIR, 'build', 'index.js')], {
  env: { ...process.env, GODOT_PATH },
  stdio: ['pipe', 'pipe', 'inherit'],
});

let buffer = '';
const pending = new Map();
let nextId = 1;

server.stdout.on('data', (chunk) => {
  buffer += chunk.toString('utf8');
  let idx;
  while ((idx = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, idx).trim();
    buffer = buffer.slice(idx + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch { continue; }
    if (msg.id !== undefined && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
    }
  }
});

function call(method, params) {
  const id = nextId++;
  return new Promise((resolvePromise) => {
    pending.set(id, resolvePromise);
    server.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  });
}

async function callTool(name, toolArgs) {
  const res = await call('tools/call', { name, arguments: toolArgs });
  if (res.error) return { error: res.error.message || JSON.stringify(res.error) };
  const text = (res.result?.content || []).map((c) => c.text).join('\n');
  return { isError: !!res.result?.isError, text };
}

// ---------- PNG 像素统计（无依赖解码，照抄 mcp_smoke_new_tools.mjs） ----------
function analyzePng(path) {
  const data = readFileSync(path);
  const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  if (!data.subarray(0, 8).equals(sig)) throw new Error('not a PNG');
  let offset = 8;
  let width = 0, height = 0, bitDepth = 0, colorType = 0;
  const idat = [];
  while (offset < data.length) {
    const len = data.readUInt32BE(offset);
    const type = data.toString('ascii', offset + 4, offset + 8);
    const body = data.subarray(offset + 8, offset + 8 + len);
    if (type === 'IHDR') {
      width = body.readUInt32BE(0);
      height = body.readUInt32BE(4);
      bitDepth = body[8];
      colorType = body[9];
    } else if (type === 'IDAT') {
      idat.push(body);
    } else if (type === 'IEND') {
      break;
    }
    offset += 12 + len;
  }
  const channels = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[colorType];
  if (bitDepth !== 8 || ![2, 6].includes(colorType)) {
    return { width, height, note: `colorType=${colorType} bitDepth=${bitDepth}, skipped pixel check` };
  }
  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * channels;
  const px = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)];
    const rowIn = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    const rowOut = px.subarray(y * stride, (y + 1) * stride);
    const prevOut = y > 0 ? px.subarray((y - 1) * stride, y * stride) : Buffer.alloc(stride);
    for (let x = 0; x < stride; x++) {
      const a = x >= channels ? rowOut[x - channels] : 0;
      const b = prevOut[x];
      const c = x >= channels ? prevOut[x - channels] : 0;
      let v = rowIn[x];
      if (filter === 1) v += a;
      else if (filter === 2) v += b;
      else if (filter === 3) v += (a + b) >> 1;
      else if (filter === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
        v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      }
      rowOut[x] = v & 0xff;
    }
  }
  let min = 255, max = 0, sum = 0, count = 0;
  const step = Math.max(1, Math.floor((width * height) / 4096));
  for (let i = 0; i < width * height; i += step) {
    const lum = px[i * channels];
    if (lum < min) min = lum;
    if (lum > max) max = lum;
    sum += lum;
    count++;
  }
  return { width, height, min, max, mean: +(sum / count).toFixed(2) };
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------
function ensureProject() {
  mkdirSync(PROJECT_PATH, { recursive: true });
  const projectFile = join(PROJECT_PATH, 'project.godot');
  if (!existsSync(projectFile)) {
    writeFileSync(projectFile, '; Engine configuration file.\n\n[application]\nconfig/name="previs_ep01"\n', 'utf8');
    console.log('created project.godot for previs_ep01');
  }
  mkdirSync(join(PROJECT_PATH, 'sets'), { recursive: true });
  mkdirSync(join(PROJECT_PATH, 'renders'), { recursive: true });
  mkdirSync(FRAMES_DIR, { recursive: true });
}

async function buildSets() {
  for (const [sceneName, def] of Object.entries(SETS)) {
    const sceneFile = join(PROJECT_PATH, def.file);
    if (existsSync(sceneFile) && !optRebuild) {
      console.log(`set ${sceneName}: exists, skip (use --rebuild to force)`);
      continue;
    }
    const t0 = Date.now();
    const res = await callTool('build_graybox_set', {
      projectPath: PROJECT_PATH,
      scenePath: def.file,
      nodes: def.nodes,
      camera: def.camera,
    });
    const ok = !res.isError && !res.error && existsSync(sceneFile);
    console.log(`set ${sceneName}: ${ok ? 'OK' : 'FAIL'} (${((Date.now() - t0) / 1000).toFixed(1)}s) ${ok ? '' : (res.text || res.error || '')}`);
    if (!ok) throw new Error(`build_graybox_set failed for ${sceneName}`);
  }
}

// ---------------------------------------------------------------------------
// 四通道导出（P1.2b）
// ---------------------------------------------------------------------------
// L3 的 ControlNet 与 L4 的 VACE 吃的是结构条件，不是彩图。这里为每个镜头
// 用**同一套内联相机**导出 rgb / depth / normal 三张（canny 由 Python 侧
// 从 rgb 后处理生成，阈值可调，比 shader 方案可控）。
// 相机必须完全一致，否则三张通道对不齐，注入下游就是错位的结构。
// 按场景分组走 render_shot_batch：godot-mcp 是 spawn-per-op，逐镜头逐通道调用
// 意味着 82×3=246 次进程启动，其中每次约 1.5s 是纯引擎启动。按场景聚合后
// 降到 5 次（每个 set 一次）。逐通道语义与单发完全一致（同一套内联相机、
// 同一套 material_override），只是场景只加载一次、进程只拆一次。
async function renderChannels(shots) {
  const results = [];
  const byScene = new Map();
  for (const shot of shots) {
    if (!SETS[shot.scene]) {
      results.push({ shot_id: shot.shot_id, status: 'failed', error: `unknown scene: ${shot.scene}` });
      console.log(`${shot.shot_id} FAIL unknown scene ${shot.scene}`);
      continue;
    }
    if (!byScene.has(shot.scene)) byScene.set(shot.scene, []);
    byScene.get(shot.scene).push(shot);
  }

  let done = 0;
  const total = shots.length - results.length;
  for (const [scene, group] of byScene) {
    const setDef = SETS[scene];
    const t0 = Date.now();
    const specs = group.map((shot) => {
      mkdirSync(join(CHANNELS_DIR, shot.shot_id), { recursive: true });
      return {
        shot_id: shot.shot_id,
        camera: computeCamera(shot, setDef),
        outputs: {
          rgb: `renders/${shot.shot_id}_rgb.png`,
          depth: `renders/${shot.shot_id}_depth.png`,
          normal: `renders/${shot.shot_id}_normal.png`,
        },
      };
    });

    const res = await callTool('render_shot_batch', {
      projectPath: PROJECT_PATH,
      scenePath: setDef.file,
      resolution: CHANNEL_RESOLUTION,
      shots: specs,
    });
    const batchErr = (res.error || res.isError)
      ? (res.text || res.error || 'batch failed').split('\n')[0] : null;

    // 逐镜头收集：批量失败也要落到镜头粒度，便于只重跑坏的那几个
    for (const [i, shot] of group.entries()) {
      const entry = { shot_id: shot.shot_id, scene, camera: specs[i].camera, channels: {} };
      let failed = batchErr;
      if (!failed) {
        for (const chan of ['rgb', 'depth', 'normal']) {
          const tmpAbs = join(PROJECT_PATH, specs[i].outputs[chan]);
          if (!existsSync(tmpAbs)) { failed = `${chan}: no output file`; break; }
          const dest = join(CHANNELS_DIR, shot.shot_id, `${chan}.png`);
          copyFileSync(tmpAbs, dest);
          unlinkSync(tmpAbs);
          try {
            entry.channels[chan] = analyzePng(dest);
          } catch (e) {
            entry.channels[chan] = { note: 'analyze failed: ' + e.message };
          }
        }
      }
      entry.status = failed ? 'failed' : 'ok';
      if (failed) entry.error = failed;
      results.push(entry);
      done++;
    }
    const secs = ((Date.now() - t0) / 1000).toFixed(1);
    const ok = group.filter((s) => results.find((r) => r.shot_id === s.shot_id)?.status === 'ok').length;
    console.log(`[${done}/${total}] scene=${scene}: ${ok}/${group.length} ok (${secs}s, 1 Godot 进程)`);
  }
  writeFileSync(CHANNELS_REPORT, JSON.stringify({
    generated_at: new Date().toISOString(),
    resolution: CHANNEL_RESOLUTION,
    total: results.length,
    ok: results.filter((r) => r.status === 'ok').length,
    failed: results.filter((r) => r.status === 'failed').length,
    results,
  }, null, 2), 'utf8');
  return results;
}

async function renderShots(shots) {
  const results = [];
  // 幂等：清掉本次涉及镜头的旧帧
  for (const shot of shots) {
    rmSync(join(FRAMES_DIR, `${shot.shot_id}.png`), { force: true });
  }

  for (let i = 0; i < shots.length; i++) {
    const shot = shots[i];
    const setDef = SETS[shot.scene];
    const t0 = Date.now();
    if (!setDef) {
      results.push({ shot_id: shot.shot_id, status: 'failed', error: `unknown scene: ${shot.scene}` });
      console.log(`[${i + 1}/${shots.length}] ${shot.shot_id} FAIL unknown scene ${shot.scene}`);
      continue;
    }
    const camera = computeCamera(shot, setDef);
    const tmpOut = `renders/${shot.shot_id}.png`;
    rmSync(join(PROJECT_PATH, tmpOut), { force: true });

    const res = await callTool('render_viewport', {
      projectPath: PROJECT_PATH,
      scenePath: setDef.file,
      outputPath: tmpOut,
      resolution: RESOLUTION,
      camera,
    });

    const tmpFile = join(PROJECT_PATH, tmpOut);
    const frameFile = join(FRAMES_DIR, `${shot.shot_id}.png`);
    let entry;
    if ((!res.isError && !res.error) && existsSync(tmpFile)) {
      copyFileSync(tmpFile, frameFile);
      unlinkSync(tmpFile);
      let pixels = null;
      try { pixels = analyzePng(frameFile); } catch (e) { pixels = { note: 'analyze failed: ' + e.message }; }
      const nonBlack = pixels && pixels.max !== undefined && pixels.max > 16 && pixels.max > pixels.min;
      entry = { shot_id: shot.shot_id, status: nonBlack ? 'ok' : 'black', frame: `frames/${shot.shot_id}.png`, pixels, camera };
    } else {
      entry = { shot_id: shot.shot_id, status: 'failed', error: (res.text || res.error || 'no output file').split('\n')[0] };
    }
    results.push(entry);
    console.log(`[${i + 1}/${shots.length}] ${shot.shot_id} ${entry.status} (${((Date.now() - t0) / 1000).toFixed(1)}s)`);
  }
  return results;
}

function writeReport(shots, results) {
  const byId = new Map(results.map((r) => [r.shot_id, r]));
  const okCount = results.filter((r) => r.status === 'ok').length;
  const blackCount = results.filter((r) => r.status === 'black').length;
  const failCount = results.filter((r) => r.status === 'failed').length;

  const report = {
    generated_at: new Date().toISOString(),
    total: results.length,
    ok: okCount,
    black: blackCount,
    failed: failCount,
    results,
  };
  writeFileSync(REPORT_JSON, JSON.stringify(report, null, 2), 'utf8');

  // 像素抽检：均匀取至少 10 帧列出 min/max/mean
  const withPixels = results.filter((r) => r.pixels && r.pixels.max !== undefined);
  const sampleN = Math.min(Math.max(10, 0), withPixels.length);
  const sample = [];
  for (let i = 0; i < sampleN; i++) {
    sample.push(withPixels[Math.floor((i * withPixels.length) / sampleN)]);
  }

  const lines = [];
  lines.push('# 灰盒预演验收报告（M0）');
  lines.push('');
  lines.push(`- 生成时间：${report.generated_at}`);
  lines.push(`- 渲染统计：共 ${report.total} 镜头，成功 ${okCount}，全黑可疑 ${blackCount}，失败 ${failCount}`);
  lines.push(`- 输入：pipeline/previs/shots.json（${shots.length} 镜头规格卡）`);
  lines.push(`- 输出：pipeline/previs/frames/<shot_id>.png（${RESOLUTION.width}x${RESOLUTION.height}）`);
  lines.push('');
  lines.push('## 像素抽检（非全黑校验，采样 min/max/mean）');
  lines.push('');
  lines.push('| shot_id | min | max | mean | 判定 |');
  lines.push('|---|---|---|---|---|');
  for (const r of sample) {
    const p = r.pixels;
    const verdict = p.max > 16 && p.max > p.min ? '非全黑 OK' : '可疑';
    lines.push(`| ${r.shot_id} | ${p.min} | ${p.max} | ${p.mean} | ${verdict} |`);
  }
  lines.push('');
  lines.push('## 镜头索引');
  lines.push('');
  lines.push('| shot_id | 组 | 场景 | 时长s | 景别 | 运镜 | 视角 | 角色 | 帧文件 | 状态 |');
  lines.push('|---|---|---|---|---|---|---|---|---|---|');
  for (const shot of shots) {
    const r = byId.get(shot.shot_id);
    const status = r ? r.status : '未渲染';
    const frame = r?.frame ? `[png](${r.frame})` : '-';
    const note = shot.is_transition ? '（转场/字幕卡，占位帧）' : '';
    lines.push(`| ${shot.shot_id} | 第${String(shot.group).padStart(2, '0')}组 | ${shot.scene} | ${shot.duration_s} | ${shot.shot_size || '-'} | ${shot.camera_move || '-'} | ${shot.perspective || '-'} | ${(shot.characters || []).join('、')}${note} | ${frame} | ${status} |`);
  }
  lines.push('');
  lines.push('## 目检结论');
  lines.push('');
  lines.push('（待人工/视觉抽检填写）');
  lines.push('');
  lines.push('## 遗留问题');
  lines.push('');
  lines.push('- 转场/黑屏/字幕卡镜头渲染的是所在场景占位帧，非真实黑场。');
  writeFileSync(INDEX_MD, lines.join('\n'), 'utf8');
  return report;
}

async function main() {
  const payload = JSON.parse(readFileSync(SHOTS_JSON, 'utf8'));
  let shots = payload.shots;
  if (optGroups) shots = shots.filter((s) => optGroups.includes(s.group));
  if (optShots) shots = shots.filter((s) => optShots.includes(s.shot_id));

  if (optReportOnly) {
    const report = existsSync(REPORT_JSON)
      ? JSON.parse(readFileSync(REPORT_JSON, 'utf8'))
      : { results: [] };
    writeReport(shots, report.results);
    console.log('report regenerated from render_report.json');
    server.kill();
    return;
  }

  ensureProject();

  const initRes = await call('initialize', {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: { name: 'previs-builder', version: '0.1.0' },
  });
  if (!initRes.result) throw new Error('initialize failed: ' + JSON.stringify(initRes));
  server.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
  await sleep(2000);

  if (!optSkipSets) await buildSets();

  if (shots.length === 0) {
    // Set-only maintenance run (e.g. --rebuild with no matching shots):
    // do not touch frames or the report
    console.log('no shots selected; sets only, report untouched');
    server.kill();
    return;
  }

  if (optChannels) {
    const chan = await renderChannels(shots);
    const ok = chan.filter((r) => r.status === 'ok').length;
    console.log(`\nchannels done: ${ok}/${chan.length} ok -> ${CHANNELS_DIR}`);
    server.kill();
    return;
  }

  const results = await renderShots(shots);
  const report = writeReport(shots, results);
  console.log(`\ndone: ${report.ok}/${report.total} ok, ${report.black} black, ${report.failed} failed`);
  console.log(`report: ${INDEX_MD}`);
  server.kill();
  process.exit(report.failed ? 1 : 0);
}

main().catch((e) => {
  console.error('build_previs crashed:', e);
  server.kill();
  process.exit(2);
});
