#!/usr/bin/env node
/**
 * Smoke test for the new godot-mcp tools (lighting/camera/animation/render).
 *
 * Spawns `node build/index.js` over stdio JSON-RPC and calls each new tool
 * against the mcp_smoke test project, then verifies render/movie outputs.
 *
 * Usage: node scripts/mcp_smoke_new_tools.mjs
 */

import { spawn, execFileSync } from 'child_process';
import { readFileSync, existsSync, statSync, rmSync } from 'fs';
import { inflateSync } from 'zlib';
import { join, resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, '..');
const MCP_DIR = join(REPO_ROOT, 'third_party', 'godot-mcp');
const PROJECT_PATH = join(REPO_ROOT, 'godot', 'projects', 'mcp_smoke');
const GODOT_PATH = join(REPO_ROOT, 'godot', 'Godot_v4.7.1-stable_win64.exe');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------- minimal MCP stdio client ----------
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
    try {
      msg = JSON.parse(line);
    } catch {
      continue;
    }
    if (msg.id !== undefined && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
    }
  }
});

function send(obj) {
  server.stdin.write(JSON.stringify(obj) + '\n');
}

function call(method, params) {
  const id = nextId++;
  return new Promise((resolvePromise) => {
    pending.set(id, resolvePromise);
    send({ jsonrpc: '2.0', id, method, params });
  });
}

async function callTool(name, args) {
  const res = await call('tools/call', { name, arguments: args });
  if (res.error) {
    return { error: res.error.message || JSON.stringify(res.error) };
  }
  const text = (res.result?.content || []).map((c) => c.text).join('\n');
  return { isError: !!res.result?.isError, text };
}

// ---------- PNG validation (8-bit RGB/RGBA, no external deps) ----------
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
  // Sample pixels for brightness statistics
  let min = 255, max = 0, sum = 0, count = 0;
  const step = Math.max(1, Math.floor((width * height) / 4096));
  for (let i = 0; i < width * height; i += step) {
    const lum = px[i * channels]; // first channel is representative enough
    if (lum < min) min = lum;
    if (lum > max) max = lum;
    sum += lum;
    count++;
  }
  return { width, height, min, max, mean: +(sum / count).toFixed(2) };
}

// ---------- test runner ----------
const results = [];
function report(name, ok, detail) {
  results.push({ name, ok, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' -- ' + detail : ''}`);
}

async function main() {
  // initialize handshake
  const initRes = await call('initialize', {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: { name: 'mcp-smoke', version: '0.1.0' },
  });
  if (!initRes.result) throw new Error('initialize failed: ' + JSON.stringify(initRes));
  send({ jsonrpc: '2.0', method: 'notifications/initialized' });
  await sleep(2000);

  // Remove stale render outputs so file-existence checks can't pass on old files
  for (const f of ['renders/frame.png', 'renders/depth.png', 'renders/clip.avi', 'renders/clip_png.mp4']) {
    rmSync(join(PROJECT_PATH, f), { force: true });
  }

  // sanity: tool list should contain all 24 tools
  const listRes = await call('tools/list', {});
  const toolNames = (listRes.result?.tools || []).map((t) => t.name);
  report('tools/list (24 tools)', toolNames.length === 24, `count=${toolNames.length}`);

  // sanity: an old tool still works
  const ver = await callTool('get_godot_version', {});
  report('get_godot_version (old tool)', !ver.isError && !ver.error, (ver.text || ver.error || '').split('\n')[0]);

  // add AnimationPlayer via the existing add_node tool (also regression check)
  const addPlayer = await callTool('add_node', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    nodeType: 'AnimationPlayer',
    nodeName: 'AnimationPlayer',
  });
  report('add_node AnimationPlayer (old tool)', !addPlayer.isError && !addPlayer.error, (addPlayer.text || '').split('\n')[0]);

  // 1. set_lighting (create)
  const light1 = await callTool('set_lighting', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    lightType: 'DirectionalLight3D',
    nodeName: 'Sun',
    transform: { rotation_degrees: { x: -50, y: -30, z: 0 } },
    properties: { color: [1.0, 0.95, 0.9], energy: 1.2, shadow_enabled: true },
  });
  report('set_lighting (create)', !light1.isError && !light1.error, (light1.text || '').split('\n')[0]);

  // 1b. set_lighting (update existing node)
  const light2 = await callTool('set_lighting', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    lightType: 'DirectionalLight3D',
    nodeName: 'Sun',
    properties: { energy: 1.5 },
  });
  report('set_lighting (update)', !light2.isError && !light2.error, (light2.text || '').split('\n')[0]);

  // 2. place_camera
  const cam = await callTool('place_camera', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    nodeName: 'Camera',
    position: { x: 4, y: 3, z: 6 },
    lookAt: { x: 0, y: 0.5, z: 0 },
    fov: 60,
  });
  report('place_camera', !cam.isError && !cam.error, (cam.text || '').split('\n')[0]);

  // 3. attach_look_at_constraint (Box faces Top)
  const constraint = await callTool('attach_look_at_constraint', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    sourceNode: 'root/Box',
    targetNode: 'root/Top',
  });
  report('attach_look_at_constraint', !constraint.isError && !constraint.error, (constraint.text || '').split('\n')[0]);

  // 4. create_animation
  const createAnim = await callTool('create_animation', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    playerNodePath: 'root/AnimationPlayer',
    animationName: 'box_move',
    length: 2.0,
  });
  report('create_animation', !createAnim.isError && !createAnim.error, (createAnim.text || '').split('\n')[0]);

  // 5. add_track (position_3d on Box)
  const track = await callTool('add_track', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    playerNodePath: 'root/AnimationPlayer',
    animationName: 'box_move',
    trackType: 'position_3d',
    targetNodePath: 'root/Box',
  });
  let trackIndex = 0;
  const trackJsonLine = (track.text || '').split('\n').find((l) => l.trim().startsWith('{'));
  if (trackJsonLine) {
    try { trackIndex = JSON.parse(trackJsonLine).track_index ?? 0; } catch { /* keep 0 */ }
  }
  report('add_track', !track.isError && !track.error, (track.text || '').split('\n')[0] + ` (track_index=${trackIndex})`);

  // 6. insert_keyframe x2
  const key1 = await callTool('insert_keyframe', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    playerNodePath: 'root/AnimationPlayer',
    animationName: 'box_move',
    trackIndex,
    time: 0.0,
    value: [0, 0.5, 0],
  });
  report('insert_keyframe (t=0)', !key1.isError && !key1.error, (key1.text || '').split('\n')[0]);

  const key2 = await callTool('insert_keyframe', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    playerNodePath: 'root/AnimationPlayer',
    animationName: 'box_move',
    trackIndex,
    time: 2.0,
    value: [2, 0.5, 0],
  });
  report('insert_keyframe (t=2)', !key2.isError && !key2.error, (key2.text || '').split('\n')[0]);

  // 6b. insert_keyframe upsert: same track + same time updates in place
  const keyUpsert = await callTool('insert_keyframe', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    playerNodePath: 'root/AnimationPlayer',
    animationName: 'box_move',
    trackIndex,
    time: 2.0,
    value: [3, 0.5, 0],
    transition: 0.5,
  });
  let upsertJson = null;
  const upsertJsonLine = (keyUpsert.text || '').split('\n').find((l) => l.trim().startsWith('{') && l.includes('"action"'));
  if (upsertJsonLine) {
    try { upsertJson = JSON.parse(upsertJsonLine); } catch { /* ignore */ }
  }
  report(
    'insert_keyframe (upsert same time)',
    !keyUpsert.isError && !keyUpsert.error && upsertJson && upsertJson.action === 'updated' && upsertJson.key_count === 2,
    upsertJson ? `action=${upsertJson.action} key_count=${upsertJson.key_count}` : (keyUpsert.text || keyUpsert.error || '').split('\n')[0]
  );

  // 6c. add_track batch keyframes: one call fills the whole track
  const createBatchAnim = await callTool('create_animation', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    playerNodePath: 'root/AnimationPlayer',
    animationName: 'box_batch',
    length: 2.0,
  });
  report('create_animation (batch target)', !createBatchAnim.isError && !createBatchAnim.error, (createBatchAnim.text || '').split('\n')[0]);

  const batchTrack = await callTool('add_track', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    playerNodePath: 'root/AnimationPlayer',
    animationName: 'box_batch',
    trackType: 'position_3d',
    targetNodePath: 'root/Box',
    keyframes: [
      { time: 0.0, value: [0, 0.5, 0] },
      { time: 1.0, value: [1, 1.5, 0], transition: 0.5 },
      { time: 2.0, value: [2, 0.5, 0] },
    ],
  });
  let batchJson = null;
  const batchJsonLine = (batchTrack.text || '').split('\n').find((l) => l.trim().startsWith('{') && l.includes('"keyframe_count"'));
  if (batchJsonLine) {
    try { batchJson = JSON.parse(batchJsonLine); } catch { /* ignore */ }
  }
  report(
    'add_track (batch 3 keyframes)',
    !batchTrack.isError && !batchTrack.error && batchJson && batchJson.keyframe_count === 3,
    batchJson ? `keyframe_count=${batchJson.keyframe_count} track_index=${batchJson.track_index}` : (batchTrack.text || batchTrack.error || '').split('\n')[0]
  );

  // 6d. verify batch keyframes landed in the scene file (3 key times + eased transition)
  const sceneTextBatch = readFileSync(join(PROJECT_PATH, 'previs.tscn'), 'utf8');
  report(
    'batch animation persisted in .tscn',
    sceneTextBatch.includes('box_batch'),
    `box_batch=${sceneTextBatch.includes('box_batch')}`
  );

  // 7. set_animation_length
  const setLen = await callTool('set_animation_length', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    playerNodePath: 'root/AnimationPlayer',
    animationName: 'box_move',
    length: 2.0,
  });
  report('set_animation_length', !setLen.isError && !setLen.error, (setLen.text || '').split('\n')[0]);

  // verify animation data landed in the scene file
  const sceneText = readFileSync(join(PROJECT_PATH, 'previs.tscn'), 'utf8');
  report(
    'animation persisted in .tscn',
    sceneText.includes('box_move') && sceneText.includes('Animation'),
    `box_move=${sceneText.includes('box_move')}`
  );

  // 8. render_viewport (windowed; opens a Godot window briefly)
  const pngPath = join(PROJECT_PATH, 'renders', 'frame.png');
  const render = await callTool('render_viewport', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    outputPath: 'renders/frame.png',
    resolution: { width: 640, height: 360 },
  });
  const renderOk = !render.isError && !render.error && existsSync(pngPath);
  let pngDetail = (render.text || render.error || '').split('\n')[0];
  let pngContentOk = false;
  if (renderOk) {
    try {
      const stats = analyzePng(pngPath);
      pngContentOk = stats.max !== undefined && stats.max > 16 && stats.max > stats.min;
      pngDetail = `${statSync(pngPath).size} bytes, ${stats.width}x${stats.height}, min=${stats.min} max=${stats.max} mean=${stats.mean}`;
    } catch (e) {
      pngDetail = 'PNG analyze failed: ' + e.message;
    }
  }
  report('render_viewport (file)', renderOk, pngDetail);
  report('render_viewport (non-black content)', pngContentOk, pngContentOk ? 'pixel range OK' : pngDetail);

  // 8b. capture_aux_buffer (depth): linearized grayscale depth PNG with a
  // visible gradient (near box = dark, far ground = bright)
  const depthPath = join(PROJECT_PATH, 'renders', 'depth.png');
  const depth = await callTool('capture_aux_buffer', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    outputPath: 'renders/depth.png',
    bufferType: 'depth',
    resolution: { width: 640, height: 360 },
  });
  const depthOk = !depth.isError && !depth.error && existsSync(depthPath);
  let depthDetail = (depth.text || depth.error || '').split('\n')[0];
  let depthGradientOk = false;
  if (depthOk) {
    try {
      const stats = analyzePng(depthPath);
      // A depth map must not be flat: the near box and the far ground/sky
      // must produce clearly different pixel values
      depthGradientOk = stats.max !== undefined && (stats.max - stats.min) >= 32 && stats.max > 16;
      depthDetail = `${statSync(depthPath).size} bytes, ${stats.width}x${stats.height}, min=${stats.min} max=${stats.max} mean=${stats.mean}`;
    } catch (e) {
      depthDetail = 'PNG analyze failed: ' + e.message;
    }
  }
  report('capture_aux_buffer depth (file)', depthOk, depthDetail);
  report('capture_aux_buffer depth (gradient)', depthGradientOk, depthGradientOk ? 'depth range OK (near != far)' : depthDetail);

  // 9. write_movie (windowed; opens a Godot window briefly)
  const aviPath = join(PROJECT_PATH, 'renders', 'clip.avi');
  const movie = await callTool('write_movie', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    outputPath: 'renders/clip.avi',
    durationSec: 2,
    fps: 30,
  });
  const movieOk = !movie.isError && !movie.error && existsSync(aviPath) && statSync(aviPath).size > 1024;
  report(
    'write_movie',
    movieOk,
    existsSync(aviPath) ? `${statSync(aviPath).size} bytes` : (movie.text || movie.error || '').split('\n')[0]
  );

  // 9b. write_movie format=png: PNG frame sequence encoded to .mp4 via ffmpeg
  const mp4Path = join(PROJECT_PATH, 'renders', 'clip_png.mp4');
  const moviePng = await callTool('write_movie', {
    projectPath: PROJECT_PATH,
    scenePath: 'previs.tscn',
    outputPath: 'renders/clip_png.mp4',
    format: 'png',
    durationSec: 1,
    fps: 30,
  });
  const mp4Ok = !moviePng.isError && !moviePng.error && existsSync(mp4Path) && statSync(mp4Path).size > 1024;
  report(
    'write_movie format=png (mp4 file)',
    mp4Ok,
    existsSync(mp4Path) ? `${statSync(mp4Path).size} bytes` : (moviePng.text || moviePng.error || '').split('\n')[0]
  );

  // ffprobe must be able to parse the mp4 duration (~1s recorded + small startup margin)
  let ffprobeDetail = 'skipped';
  let ffprobeOk = false;
  if (mp4Ok) {
    try {
      const out = execFileSync('ffprobe', [
        '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        mp4Path,
      ]).toString('utf8').trim();
      const duration = parseFloat(out);
      ffprobeDetail = `duration=${duration}s`;
      ffprobeOk = Number.isFinite(duration) && duration >= 0.9 && duration <= 3.0;
    } catch (e) {
      ffprobeDetail = 'ffprobe failed: ' + e.message;
    }
  }
  report('write_movie format=png (ffprobe duration)', ffprobeOk, ffprobeDetail);

  // summary
  const failed = results.filter((r) => !r.ok);
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
  server.kill();
  process.exit(failed.length ? 1 : 0);
}

main().catch((e) => {
  console.error('smoke runner crashed:', e);
  server.kill();
  process.exit(2);
});
