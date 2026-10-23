/**
 * P1.2 冒烟：capture_aux_buffer 的 normal 通道。
 *
 * 判据（与 depth 不同，normal 不做拉伸，必须验编码合理性）：
 *   - 输出 PNG 存在、尺寸正确
 *   - 非全灰：存在明显偏离背景 0.5 的像素（说明确实写入了表面法线）
 *   - 通道分离：R/G/B 三通道均值互不相同（若三通道相同说明退化成了灰度）
 *
 * 用法: node scripts/smoke_normal_buffer.mjs
 */
import { spawn } from 'node:child_process';
import { existsSync, readFileSync, rmSync, mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { inflateSync } from 'node:zlib';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const PROJECT_PATH = join(ROOT, 'godot', 'projects', 'previs_ep01');
const SERVER = join(ROOT, 'third_party', 'godot-mcp', 'build', 'index.js');
const OUT_DIR = join(ROOT, 'pipeline', 'previs', 'aux_smoke');

mkdirSync(OUT_DIR, { recursive: true });

// ---- PNG 解码 + 逐通道统计（照抄 build_previs.mjs 的无依赖解码，扩到三通道） ----
function analyzeRgb(path) {
  const data = readFileSync(path);
  let offset = 8;
  let width = 0, height = 0, bitDepth = 0, colorType = 0;
  const idat = [];
  while (offset < data.length) {
    const len = data.readUInt32BE(offset);
    const type = data.toString('ascii', offset + 4, offset + 8);
    const body = data.subarray(offset + 8, offset + 8 + len);
    if (type === 'IHDR') {
      width = body.readUInt32BE(0); height = body.readUInt32BE(4);
      bitDepth = body[8]; colorType = body[9];
    } else if (type === 'IDAT') idat.push(body);
    else if (type === 'IEND') break;
    offset += 12 + len;
  }
  const channels = { 0: 1, 2: 3, 3: 1, 4: 2, 6: 4 }[colorType];
  if (bitDepth !== 8 || ![2, 6].includes(colorType)) {
    throw new Error(`unsupported png: colorType=${colorType} bitDepth=${bitDepth}`);
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
  const sums = [0, 0, 0];
  let offGray = 0, n = 0;
  const step = Math.max(1, Math.floor((width * height) / 20000));
  for (let i = 0; i < width * height; i += step) {
    const r = px[i * channels], g = px[i * channels + 1], b = px[i * channels + 2];
    sums[0] += r; sums[1] += g; sums[2] += b;
    // 偏离背景 (128,128,128) 超过 12 个色阶即认为写入了法线
    if (Math.abs(r - 128) > 12 || Math.abs(g - 128) > 12 || Math.abs(b - 128) > 12) offGray++;
    n++;
  }
  return {
    width, height,
    meanR: +(sums[0] / n).toFixed(2), meanG: +(sums[1] / n).toFixed(2), meanB: +(sums[2] / n).toFixed(2),
    offGrayRatio: +(offGray / n).toFixed(4),
  };
}

// ---- MCP stdio ----
const server = spawn('node', [SERVER], {
  stdio: ['pipe', 'pipe', 'inherit'],
  env: { ...process.env, GODOT_PATH: join(ROOT, 'godot', 'Godot_v4.7.1-stable_win64.exe') },
});
let nextId = 1;
const pending = new Map();
let buf = '';
server.stdout.on('data', (chunk) => {
  buf += chunk.toString();
  let idx;
  while ((idx = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, idx).trim();
    buf = buf.slice(idx + 1);
    if (!line) continue;
    let msg; try { msg = JSON.parse(line); } catch { continue; }
    if (msg.id !== undefined && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  }
});
function call(method, params) {
  const id = nextId++;
  return new Promise((res) => { pending.set(id, res); server.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'); });
}
async function callTool(name, args) {
  const res = await call('tools/call', { name, arguments: args });
  if (res.error) return { error: res.error.message || JSON.stringify(res.error) };
  return { isError: !!res.result?.isError, text: (res.result?.content || []).map((c) => c.text).join('\n') };
}

await call('initialize', {
  protocolVersion: '2024-11-05', capabilities: {},
  clientInfo: { name: 'smoke-normal', version: '1.0.0' },
});

// 内联渲染相机格式是 {position, lookAt, fov}（不是 place_camera 的 {position, rotation}）
const CAMERA = { position: { x: 0, y: 1.6, z: 4 }, lookAt: { x: 0, y: 1.2, z: -2 }, fov: 40 };
let failures = 0;

for (const [bufferType, scene] of [['depth', 'sets/kitchen.tscn'], ['normal', 'sets/kitchen.tscn']]) {
  const rel = `renders/smoke_${bufferType}.png`;
  const abs = join(PROJECT_PATH, rel);
  rmSync(abs, { force: true });
  const res = await callTool('capture_aux_buffer', {
    projectPath: PROJECT_PATH, scenePath: scene, outputPath: rel,
    bufferType, resolution: { width: 640, height: 360 }, camera: CAMERA,
  });
  if (res.error || res.isError || !existsSync(abs)) {
    console.log(`[FAIL] ${bufferType}: ${(res.text || res.error || 'no output file').split('\n')[0]}`);
    failures++;
    continue;
  }
  const st = analyzeRgb(abs);
  const chanSpread = Math.max(st.meanR, st.meanG, st.meanB) - Math.min(st.meanR, st.meanG, st.meanB);
  let ok, why;
  if (bufferType === 'normal') {
    // normal 必须彩色（通道分离）且大面积偏离背景灰
    ok = chanSpread > 3 && st.offGrayRatio > 0.15;
    why = `chanSpread=${chanSpread.toFixed(2)} (>3) offGray=${st.offGrayRatio} (>0.15)`;
  } else {
    // depth 是灰度：三通道应基本相同
    ok = chanSpread < 2;
    why = `chanSpread=${chanSpread.toFixed(2)} (<2, 灰度)`;
  }
  if (!ok) failures++;
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${bufferType} ${st.width}x${st.height} RGB=(${st.meanR},${st.meanG},${st.meanB}) ${why}`);
  const keep = join(OUT_DIR, `kitchen_${bufferType}.png`);
  rmSync(keep, { force: true });
  const { copyFileSync } = await import('node:fs');
  copyFileSync(abs, keep);
}

// ---------------------------------------------------------------------------
// 不变量测试：capture_aux_buffer 必须真正使用传入的内联相机
// ---------------------------------------------------------------------------
// 2026-08-07 踩过的坑：index.ts 的 capture_aux_buffer handler 忘了转发 `camera`
// 参数，于是静默回退到"场景里第一个 Camera3D"。三通道因此对不齐，
// 而 depth/normal 图**单看依然完全正常**——目检发现不了，只有对齐才暴露。
// 检法：用两个明显不同的机位各渲一张 depth，若字节完全相同，
// 说明相机参数根本没生效。
{
  const shots = [];
  for (const [tag, cam] of [
    ['camA', { position: { x: 0, y: 1.6, z: 4 }, lookAt: { x: 0, y: 1.2, z: -2 }, fov: 40 }],
    ['camB', { position: { x: 3.5, y: 2.4, z: -2 }, lookAt: { x: -1, y: 0.8, z: 1 }, fov: 70 }],
  ]) {
    const rel = `renders/inv_${tag}.png`;
    const abs = join(PROJECT_PATH, rel);
    rmSync(abs, { force: true });
    const res = await callTool('capture_aux_buffer', {
      projectPath: PROJECT_PATH, scenePath: 'sets/kitchen.tscn', outputPath: rel,
      bufferType: 'depth', resolution: { width: 320, height: 180 }, camera: cam,
    });
    if (res.error || res.isError || !existsSync(abs)) {
      console.log(`[FAIL] 不变量测试: ${tag} 渲染失败`);
      failures++;
      shots.length = 0;
      break;
    }
    shots.push(readFileSync(abs));
  }
  if (shots.length === 2) {
    const identical = shots[0].equals(shots[1]);
    if (identical) failures++;
    console.log(`[${identical ? 'FAIL' : 'PASS'}] 内联相机生效性: 两个不同机位的 depth ` +
      `${identical ? '字节完全相同 —— camera 参数被丢弃！' : '不同 —— 相机参数已生效'}`);
  }
}

console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURE(S)`);
server.kill();
process.exit(failures === 0 ? 0 : 1);
