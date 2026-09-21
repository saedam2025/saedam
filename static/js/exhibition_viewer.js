/*
 * [통합관리] > 3D전시장 관람 화면.
 *
 * 서버가 내려준 전시관 좌표(scene JSON)로 3D 공간을 만들고,
 * PC 방향키 · 모바일 조이스틱 · VR 조작스틱으로 걸어 다니며 관람한다.
 * 작품을 클릭하면 사진은 슬라이드 쇼로, 동영상은 전체화면으로 재생된다.
 */

// three.js는 인터넷이 막힌 망에서도 열리도록 프로젝트 안에 내려받아 쓴다.
// (static/js/vendor/three, r161 · MIT)
import * as THREE from './vendor/three/three.module.js';
import { buildEnvironment, createFrame, disposeFrame } from './exhibition_environment.js';

const dataNode = document.getElementById('exSceneData');
if (!dataNode) throw new Error('전시관 데이터가 없습니다.');
const SCENE = JSON.parse(dataNode.textContent);

const stage = document.getElementById('exStage');
const gate = document.getElementById('exGate');
const hud = document.getElementById('exHud');
const hint = document.getElementById('exHint');
const touchPad = document.getElementById('exTouch');
const joystick = document.getElementById('exJoystick');
const joystickKnob = document.getElementById('exJoystickKnob');
const reticle = document.getElementById('exReticle');
const focusLabel = document.getElementById('exFocusLabel');
const loading = document.getElementById('exLoading');
const loadingText = document.getElementById('exLoadingText');
const loadingBar = document.getElementById('exLoadingBar');
const enterBtn = document.getElementById('exEnterBtn');
const bgmBtn = document.getElementById('exBgmBtn');
const fullBtn = document.getElementById('exFullBtn');
const eyeBtn = document.getElementById('exEyeBtn');
const eyePanel = document.getElementById('exEyePanel');
const eyeRange = document.getElementById('exEyeRange');
const eyeValue = document.getElementById('exEyeValue');
const exitLink = document.getElementById('exExitLink');

const modal = document.getElementById('exModal');
const modalStage = document.getElementById('exModalStage');
const modalCaption = document.getElementById('exModalCaption');
const modalClose = document.getElementById('exModalClose');
const modalPrev = document.getElementById('exModalPrev');
const modalNext = document.getElementById('exModalNext');

const HALL = SCENE.hall;
const LAYOUT = SCENE.layout;
const SLOTS = SCENE.slots.filter((slot) => slot.media && slot.media.length);

const EYE_HEIGHT = 1.62;         // 기본 눈높이(m) — 시점 높이 조절의 기준
const EYE_MIN = 0.90;            // 앉은 키 정도
const EYE_MAX = 2.40;            // 내려다보는 정도
const EYE_STEP = 0.05;
const EYE_STORE_KEY = 'exhibition:eyeHeight';
const EYE_KEYS = { PageUp: 1, PageDown: -1, ']': 1, '[': -1 };
const BASE_SPEED = 3.1;          // m/s
const TURN_SPEED = 1.9;          // rad/s (방향키 회전)
const WALL_MARGIN = 0.65;
const REACH = 9.5;               // 작품을 집을 수 있는 거리
const VIDEO_PREVIEW_DISTANCE = 13;
const MAX_PREVIEW_VIDEOS = 3;

const isTouchDevice = window.matchMedia('(pointer: coarse)').matches;

// 관람 중에는 뒤쪽 문서가 스크롤되지 않도록 한다.
document.documentElement.style.overflow = 'hidden';

/* ------------------------------------------------------------------ */
/* 기본 3D 구성                                                         */
/* ------------------------------------------------------------------ */

let renderer;
try {
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
} catch (error) {
  loadingText.textContent = '이 브라우저에서는 3D 전시장을 열 수 없습니다.';
  throw error;
}
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, isTouchDevice ? 1.5 : 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.xr.enabled = true;
stage.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0b1220);
scene.fog = new THREE.Fog(0x0b1220, 34, 74);

// 시점 높이는 사람마다 편한 정도가 달라서 기기에 저장해 두고 다음에도 그대로 쓴다.
function loadEyeHeight() {
  try {
    const saved = parseFloat(window.localStorage.getItem(EYE_STORE_KEY));
    if (isFinite(saved)) return Math.max(EYE_MIN, Math.min(EYE_MAX, saved));
  } catch (error) { /* 저장이 막힌 브라우저면 기본값으로 연다 */ }
  return EYE_HEIGHT;
}
let eyeHeight = loadEyeHeight();

const camera = new THREE.PerspectiveCamera(62, window.innerWidth / window.innerHeight, 0.1, 220);
camera.position.set(0, eyeHeight, 0);

// 사람(이동 기준). VR에서는 이 그룹이 통째로 움직이고 머리 방향은 기기가 정한다.
const player = new THREE.Group();
player.position.set(LAYOUT.spawn.x, 0, LAYOUT.spawn.z);
player.rotation.y = LAYOUT.spawn.heading || 0;
player.add(camera);
scene.add(player);

const HALF_W = LAYOUT.size.width / 2;
const HALF_D = LAYOUT.size.depth / 2;
const HEIGHT = LAYOUT.size.height;
const accent = new THREE.Color(HALL.accentColor || '#2563eb');

/* ------------------------------------------------------------------ */
/* 조명                                                                */
/* ------------------------------------------------------------------ */

// 전시공간(작품 자리) 목록은 서버가 layout 바깥(SCENE.slots)으로 내려주므로
// 조명을 걸 때 쓸 수 있도록 layout에 합쳐서 넘긴다.
const { blockers, frameMaterial, ready: environmentReady } = buildEnvironment(
  scene, renderer, HALL, { ...LAYOUT, slots: SCENE.slots }, isTouchDevice,
);

const artworks = [];          // { slot, mesh, media, index, video, placard }
const pickTargets = [];
const textureLoader = new THREE.TextureLoader();
const canvasTextureCache = new Map();

function makePlacardTexture(slot) {
  const key = `${slot.code}|${slot.title}|${slot.artist}`;
  if (canvasTextureCache.has(key)) return canvasTextureCache.get(key);

  const canvas = document.createElement('canvas');
  canvas.width = 720;
  canvas.height = 220;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = `#${accent.getHexString()}`;
  ctx.fillRect(0, 0, 10, canvas.height);
  ctx.fillStyle = '#0f172a';
  ctx.font = 'bold 58px "Malgun Gothic", "Noto Sans KR", sans-serif';
  ctx.textBaseline = 'top';
  const title = slot.title || '제목 없는 작품';
  ctx.fillText(title.length > 16 ? `${title.slice(0, 16)}…` : title, 38, 34);
  ctx.fillStyle = '#475569';
  ctx.font = '42px "Malgun Gothic", "Noto Sans KR", sans-serif';
  const artist = slot.artist || '';
  ctx.fillText(artist.length > 22 ? `${artist.slice(0, 22)}…` : artist, 38, 116);

  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  canvasTextureCache.set(key, texture);
  return texture;
}

function fitSize(slot, aspect) {
  // 전시 자리 안에 비율을 지키며 최대한 크게 넣는다.
  const safeAspect = aspect && isFinite(aspect) && aspect > 0 ? aspect : 4 / 3;
  let width = slot.width;
  let height = width / safeAspect;
  if (height > slot.height) {
    height = slot.height;
    width = height * safeAspect;
  }
  return { width, height };
}

function createVideoElement(url) {
  const video = document.createElement('video');
  video.src = url;
  video.crossOrigin = 'use-credentials';
  video.loop = true;
  video.muted = true;
  video.playsInline = true;
  video.preload = 'metadata';
  video.setAttribute('playsinline', '');
  return video;
}

function buildArtwork(slot) {
  const first = slot.media[0];
  const aspect = first.kind === 'image' && first.width && first.height
    ? first.width / first.height
    : 16 / 9;
  const size = fitSize(slot, aspect);

  const group = new THREE.Group();
  group.position.set(slot.x, slot.y, slot.z);
  group.rotation.y = slot.rotationY;

  // 칠판처럼 액자 없이 전시물만 보여야 하는 자리가 있다.
  let frame = null;
  if (!slot.frameless) {
    frame = createFrame(size.width, size.height, frameMaterial);
    frame.userData.sharedMaterial = frameMaterial;
    group.add(frame);
  }
  const material = new THREE.MeshBasicMaterial({ color: 0x222222, toneMapped: false });
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(size.width, size.height), material);
  mesh.position.z = 0.005;
  group.add(mesh);

  const artwork = {
    slot,
    group,
    mesh,
    frame,
    material,
    size,
    index: 0,
    video: null,
    userPlaying: false,
    lastSlideAt: 0,
    textures: new Map(),
  };
  mesh.userData.artwork = artwork;
  pickTargets.push(mesh);

  // 작품명 팻말
  if (HALL.placardEnabled && (slot.title || slot.artist)) {
    const placard = new THREE.Mesh(
      new THREE.PlaneGeometry(1.05, 0.32),
      new THREE.MeshBasicMaterial({ map: makePlacardTexture(slot), transparent: false }),
    );
    placard.position.set(0, -(size.height / 2) - 0.43, 0.01);
    group.add(placard);
    artwork.placard = placard;
  }

  scene.add(group);
  artworks.push(artwork);
  return artwork;
}

function loadTexture(url) {
  return new Promise((resolve, reject) => {
    textureLoader.load(
      url,
      (texture) => {
        texture.colorSpace = THREE.SRGBColorSpace;
        texture.anisotropy = Math.min(renderer.capabilities.getMaxAnisotropy(), 8);
        resolve(texture);
      },
      undefined,
      reject,
    );
  });
}

function refit(artwork, aspect) {
  const size = fitSize(artwork.slot, aspect);
  if (Math.abs(size.width - artwork.size.width) < 0.01
      && Math.abs(size.height - artwork.size.height) < 0.01) return;
  artwork.mesh.geometry.dispose();
  artwork.mesh.geometry = new THREE.PlaneGeometry(size.width, size.height);
  artwork.size = size;
  if (artwork.frame) {
    artwork.group.remove(artwork.frame);
    disposeFrame(artwork.frame);
    artwork.frame = createFrame(size.width, size.height, frameMaterial);
    artwork.frame.userData.sharedMaterial = frameMaterial;
    artwork.group.add(artwork.frame);
  }
  if (artwork.placard) {
    artwork.placard.position.set(0, -(size.height / 2) - 0.43, 0.01);
  }
}


async function showMedia(artwork, index) {
  const media = artwork.slot.media[index];
  if (!media) return;
  artwork.index = index;

  if (media.kind === 'video') {
    if (!artwork.video) {
      artwork.video = createVideoElement(media.url);
      const texture = new THREE.VideoTexture(artwork.video);
      texture.colorSpace = THREE.SRGBColorSpace;
      artwork.videoTexture = texture;
      // 화면 크기를 알게 되는 순간 액자 비율을 맞춘다.
      artwork.video.addEventListener('loadedmetadata', () => {
        if (artwork.video.videoWidth) {
          refit(artwork, artwork.video.videoWidth / artwork.video.videoHeight);
        }
      }, { once: true });
    }
    artwork.material.map = artwork.videoTexture;
    artwork.material.color.set(0xffffff);
    artwork.material.needsUpdate = true;
    if (artwork.video.videoWidth) {
      refit(artwork, artwork.video.videoWidth / artwork.video.videoHeight);
    }
    return;
  }

  let texture = artwork.textures.get(media.id);
  if (!texture) {
    try {
      texture = await loadTexture(media.url);
    } catch (error) {
      return;
    }
    artwork.textures.set(media.id, texture);
  }
  artwork.material.map = texture;
  artwork.material.color.set(0xffffff);
  artwork.material.needsUpdate = true;

  // 사진마다 가로·세로 비율이 다르므로 바뀔 때마다 액자를 다시 맞춘다.
  if (texture.image && texture.image.width) {
    refit(artwork, texture.image.width / texture.image.height);
  }
}

async function prepareArtworks() {
  SLOTS.forEach(buildArtwork);
  let done = 0;
  const total = Math.max(artworks.length, 1);
  for (const artwork of artworks) {
    await showMedia(artwork, 0);
    done += 1;
    loadingBar.style.width = `${Math.round((done / total) * 100)}%`;
    loadingText.textContent = `작품을 거는 중… (${done}/${artworks.length})`;
  }
}

/* ------------------------------------------------------------------ */
/* 이동 · 시점                                                          */
/* ------------------------------------------------------------------ */

const keys = new Set();
let pitch = 0;
let running = false;
const move = { x: 0, y: 0 };            // 조이스틱/스틱 입력 (-1 ~ 1)
let turnInput = 0;
let heightInput = 0;

window.addEventListener('keydown', (event) => {
  // 시점 높이: PageUp / PageDown 또는 ] / [ (누르고 있으면 계속 조절된다)
  if (Object.prototype.hasOwnProperty.call(EYE_KEYS, event.key)) {
    setEyeHeight(eyeHeight + EYE_KEYS[event.key] * EYE_STEP, { notice: false });
    event.preventDefault();
    return;
  }
  if (event.repeat) return;
  if (event.key === 'Escape') { closeModal(); return; }
  keys.add(event.key.toLowerCase());
  running = event.shiftKey;
  if (['arrowup', 'arrowdown', 'arrowleft', 'arrowright', ' '].includes(event.key.toLowerCase())) {
    event.preventDefault();
  }
});
window.addEventListener('keyup', (event) => {
  keys.delete(event.key.toLowerCase());
  running = event.shiftKey;
});
window.addEventListener('blur', () => keys.clear());

function keyAxis() {
  const forward = (keys.has('arrowup') || keys.has('w') ? 1 : 0)
    - (keys.has('arrowdown') || keys.has('s') ? 1 : 0);
  const strafe = (keys.has('d') ? 1 : 0) - (keys.has('a') ? 1 : 0);
  const turn = (keys.has('arrowleft') ? 1 : 0) - (keys.has('arrowright') ? 1 : 0);
  return { forward, strafe, turn };
}

// 마우스 드래그로 둘러보기(짧게 누르면 작품 선택)
let dragging = false;
let dragMoved = 0;
let lastPointer = { x: 0, y: 0 };

renderer.domElement.addEventListener('pointerdown', (event) => {
  if (event.pointerType === 'touch') return;
  dragging = true;
  dragMoved = 0;
  lastPointer = { x: event.clientX, y: event.clientY };
  renderer.domElement.setPointerCapture(event.pointerId);
});

renderer.domElement.addEventListener('pointermove', (event) => {
  if (!dragging || event.pointerType === 'touch') return;
  const dx = event.clientX - lastPointer.x;
  const dy = event.clientY - lastPointer.y;
  lastPointer = { x: event.clientX, y: event.clientY };
  dragMoved += Math.abs(dx) + Math.abs(dy);
  applyLook(dx, dy);
});

renderer.domElement.addEventListener('pointerup', (event) => {
  if (event.pointerType === 'touch') return;
  dragging = false;
  if (dragMoved < 7) pickAt(event.clientX, event.clientY);
});

function applyLook(dx, dy) {
  player.rotation.y -= dx * 0.0032;
  pitch = Math.max(-1.05, Math.min(1.05, pitch - dy * 0.0028));
  camera.rotation.x = pitch;
}

/* 모바일: 왼쪽 조이스틱 이동 + 화면 쓸어 둘러보기 */
let joystickId = null;
let joystickOrigin = { x: 0, y: 0 };
let lookTouchId = null;
let lookLast = { x: 0, y: 0 };
let lookMoved = 0;

joystick.addEventListener('pointerdown', (event) => {
  joystickId = event.pointerId;
  const rect = joystick.getBoundingClientRect();
  joystickOrigin = { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  joystick.setPointerCapture(event.pointerId);
  updateJoystick(event.clientX, event.clientY);
  event.preventDefault();
});
joystick.addEventListener('pointermove', (event) => {
  if (event.pointerId !== joystickId) return;
  updateJoystick(event.clientX, event.clientY);
});
['pointerup', 'pointercancel'].forEach((type) => {
  joystick.addEventListener(type, (event) => {
    if (event.pointerId !== joystickId) return;
    joystickId = null;
    move.x = 0;
    move.y = 0;
    joystickKnob.style.transform = '';
  });
});

function updateJoystick(x, y) {
  const limit = 46;
  let dx = x - joystickOrigin.x;
  let dy = y - joystickOrigin.y;
  const length = Math.hypot(dx, dy) || 1;
  if (length > limit) {
    dx = (dx / length) * limit;
    dy = (dy / length) * limit;
  }
  joystickKnob.style.transform = `translate(${dx}px, ${dy}px)`;
  move.x = dx / limit;
  move.y = -dy / limit;
}

renderer.domElement.addEventListener('touchstart', (event) => {
  const touch = event.changedTouches[0];
  if (lookTouchId !== null || !touch) return;
  lookTouchId = touch.identifier;
  lookLast = { x: touch.clientX, y: touch.clientY };
  lookMoved = 0;
}, { passive: true });

renderer.domElement.addEventListener('touchmove', (event) => {
  for (const touch of event.changedTouches) {
    if (touch.identifier !== lookTouchId) continue;
    const dx = touch.clientX - lookLast.x;
    const dy = touch.clientY - lookLast.y;
    lookLast = { x: touch.clientX, y: touch.clientY };
    lookMoved += Math.abs(dx) + Math.abs(dy);
    applyLook(dx * 1.25, dy * 1.25);
  }
}, { passive: true });

renderer.domElement.addEventListener('touchend', (event) => {
  for (const touch of event.changedTouches) {
    if (touch.identifier !== lookTouchId) continue;
    lookTouchId = null;
    if (lookMoved < 12) pickAt(touch.clientX, touch.clientY);
  }
}, { passive: true });

/* ------------------------------------------------------------------ */
/* 충돌 처리                                                            */
/* ------------------------------------------------------------------ */

function clampPosition(next) {
  next.x = Math.max(-HALF_W + WALL_MARGIN, Math.min(HALF_W - WALL_MARGIN, next.x));
  next.z = Math.max(-HALF_D + WALL_MARGIN, Math.min(HALF_D - WALL_MARGIN, next.z));

  blockers.forEach((box) => {
    if (next.x < box.minX || next.x > box.maxX || next.z < box.minZ || next.z > box.maxZ) return;
    // 가장 가까운 면으로 밀어낸다.
    const push = [
      { axis: 'x', value: box.minX, distance: next.x - box.minX },
      { axis: 'x', value: box.maxX, distance: box.maxX - next.x },
      { axis: 'z', value: box.minZ, distance: next.z - box.minZ },
      { axis: 'z', value: box.maxZ, distance: box.maxZ - next.z },
    ].sort((a, b) => a.distance - b.distance)[0];
    next[push.axis] = push.value;
  });
  return next;
}

/* ------------------------------------------------------------------ */
/* 작품 선택(클릭·터치·VR 조준)                                          */
/* ------------------------------------------------------------------ */

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();

function pickAt(clientX, clientY) {
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(pickTargets, false)[0];
  if (hit && hit.distance <= REACH) openArtwork(hit.object.userData.artwork);
}

function centreTarget() {
  pointer.set(0, 0);
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObjects(pickTargets, false)[0];
  return hit && hit.distance <= REACH ? hit.object.userData.artwork : null;
}

/* ------------------------------------------------------------------ */
/* 작품 상세 (사진 슬라이드 쇼 / 동영상 전체화면)                          */
/* ------------------------------------------------------------------ */

let modalArtwork = null;
let modalIndex = 0;
let modalTimer = null;
let modalVideo = null;

// 동영상은 액자 안에서 그대로 튼다. 누를 때마다 재생 <-> 멈춤.
function toggleVideo(artwork) {
  const video = artwork.video;
  if (!video) return;
  if (artwork.userPlaying) {
    artwork.userPlaying = false;
    video.pause();
    video.muted = true;
    duckBgm(false);
    return;
  }
  // 소리가 겹치지 않도록 다른 자리에서 틀어 둔 동영상은 멈춘다.
  artworks.forEach((other) => {
    if (other === artwork || !other.userPlaying) return;
    other.userPlaying = false;
    other.video.pause();
    other.video.muted = true;
  });
  artwork.userPlaying = true;
  video.muted = false;
  video.play().catch(() => {
    // 브라우저가 소리 있는 재생을 막으면 소리 없이라도 이어서 튼다.
    video.muted = true;
    video.play().catch(() => {});
  });
  duckBgm(true);
}

function openArtwork(artwork) {
  if (!artwork) return;
  const current = artwork.slot.media[artwork.index || 0];
  if (current && current.kind === 'video') { toggleVideo(artwork); return; }
  modalArtwork = artwork;
  modalIndex = artwork.index || 0;
  modal.classList.add('open');
  renderModalItem();
}

function clearModalStage() {
  if (modalTimer) { clearInterval(modalTimer); modalTimer = null; }
  if (modalVideo) { modalVideo.pause(); modalVideo = null; }
  modalStage.querySelectorAll('img, video, .ex-modal-count').forEach((node) => node.remove());
}

function renderModalItem() {
  if (!modalArtwork) return;
  clearModalStage();

  const list = modalArtwork.slot.media;
  const media = list[modalIndex];
  if (!media) return;
  const multiple = list.length > 1;
  modalPrev.hidden = !multiple;
  modalNext.hidden = !multiple;

  if (media.kind === 'video') {
    const video = document.createElement('video');
    video.src = media.url;
    video.controls = true;
    video.autoplay = true;
    video.playsInline = true;
    video.setAttribute('playsinline', '');
    modalStage.appendChild(video);
    modalVideo = video;
    duckBgm(true);
    // 전체화면을 지원하면 바로 전체화면으로 띄운다.
    video.addEventListener('loadedmetadata', () => {
      const request = video.requestFullscreen || video.webkitEnterFullscreen
        || video.webkitRequestFullscreen;
      if (request) {
        try { request.call(video); } catch (error) { /* 브라우저가 막으면 창 안에서 재생 */ }
      }
    }, { once: true });
  } else {
    const image = document.createElement('img');
    image.src = media.url;
    image.alt = modalArtwork.slot.title || '전시 작품';
    modalStage.appendChild(image);
    // 사진이 여러 장이면 슬라이드 쇼로 자동으로 넘긴다.
    if (multiple) {
      modalTimer = setInterval(() => stepModal(1), Math.max(HALL.slideSeconds, 2) * 1000);
    }
  }

  const slot = modalArtwork.slot;
  modalCaption.innerHTML = '';
  const title = document.createElement('strong');
  title.textContent = slot.title || '제목 없는 작품';
  const artist = document.createElement('span');
  artist.textContent = slot.artist || '';
  modalCaption.append(title, artist);
  if (slot.description) {
    const description = document.createElement('p');
    description.textContent = slot.description;
    modalCaption.appendChild(description);
  }
  if (multiple) {
    const dots = document.createElement('div');
    dots.className = 'ex-modal-count';
    list.forEach((_item, index) => {
      const dot = document.createElement('i');
      if (index === modalIndex) dot.className = 'on';
      dots.appendChild(dot);
    });
    modalCaption.appendChild(dots);
  }

  // 벽에 걸린 작품도 같은 장으로 맞춰 준다.
  showMedia(modalArtwork, modalIndex);
}

function stepModal(direction) {
  if (!modalArtwork) return;
  const list = modalArtwork.slot.media;
  modalIndex = (modalIndex + direction + list.length) % list.length;
  renderModalItem();
}

function closeModal() {
  if (!modal.classList.contains('open')) return;
  clearModalStage();
  modal.classList.remove('open');
  modalArtwork = null;
  duckBgm(false);
  if (document.fullscreenElement && document.fullscreenElement !== stage) {
    document.exitFullscreen().catch(() => {});
  }
}

modalClose.addEventListener('click', closeModal);
modalPrev.addEventListener('click', () => stepModal(-1));
modalNext.addEventListener('click', () => stepModal(1));
modal.addEventListener('click', (event) => { if (event.target === modal) closeModal(); });
window.addEventListener('keydown', (event) => {
  if (!modal.classList.contains('open')) return;
  if (event.key === 'ArrowRight') stepModal(1);
  if (event.key === 'ArrowLeft') stepModal(-1);
});

/* ------------------------------------------------------------------ */
/* 배경음악                                                             */
/* ------------------------------------------------------------------ */

let bgm = null;
let bgmOn = false;

function setupBgm() {
  if (!HALL.bgmUrl) {
    bgmBtn.hidden = true;
    return;
  }
  bgm = new Audio(HALL.bgmUrl);
  bgm.loop = true;
  bgm.volume = Math.max(0, Math.min(1, HALL.bgmVolume));
  bgmBtn.addEventListener('click', () => (bgmOn ? stopBgm() : startBgm()));
}

function startBgm() {
  if (!bgm) return;
  bgm.play().then(() => {
    bgmOn = true;
    bgmBtn.classList.remove('off');
  }).catch(() => {
    bgmOn = false;
    bgmBtn.classList.add('off');
  });
}

function stopBgm() {
  if (!bgm) return;
  bgm.pause();
  bgmOn = false;
  bgmBtn.classList.add('off');
}

function duckBgm(quiet) {
  if (!bgm || !bgmOn) return;
  bgm.volume = quiet
    ? Math.max(0, Math.min(1, HALL.bgmVolume)) * 0.25
    : Math.max(0, Math.min(1, HALL.bgmVolume));
}

/* ------------------------------------------------------------------ */
/* 시점 높이(눈높이)                                                     */
/* ------------------------------------------------------------------ */

// VR 기기가 바닥 높이를 제대로 알려주지 못하면 시점이 바닥에 붙어 버린다.
// 입장 직후 머리 높이를 한 번 재서 모자란 만큼을 자동으로 채워 준다.
let vrFloorFix = 0;
let vrFloorFrames = 0;
let vrFloorChecked = false;
let eyeSaveTimer = null;
let eyeNotice = null;
let eyeNoticeUntil = 0;

function applyEyeHeight() {
  if (renderer.xr.isPresenting) {
    // VR에서는 머리 높이를 기기가 정하므로 사람(그룹)을 통째로 올리고 내린다.
    player.position.y = vrFloorFix + (eyeHeight - EYE_HEIGHT);
  } else {
    player.position.y = 0;
    camera.position.y = eyeHeight;
  }
}

function saveEyeHeightLater() {
  if (eyeSaveTimer) clearTimeout(eyeSaveTimer);
  eyeSaveTimer = setTimeout(() => {
    try { window.localStorage.setItem(EYE_STORE_KEY, eyeHeight.toFixed(2)); }
    catch (error) { /* 저장이 막혀도 이번 관람에는 그대로 적용된다 */ }
  }, 400);
}

function updateEyeUi() {
  if (eyeValue) eyeValue.textContent = `${eyeHeight.toFixed(2)} m`;
  if (eyeRange && Math.abs(parseFloat(eyeRange.value) - eyeHeight) > 0.005) {
    eyeRange.value = eyeHeight.toFixed(2);
  }
  if (eyePanel) {
    eyePanel.querySelectorAll('[data-eye-preset]').forEach((button) => {
      const preset = parseFloat(button.dataset.eyePreset);
      button.classList.toggle('on', Math.abs(preset - eyeHeight) < 0.02);
    });
  }
}

function setEyeHeight(value, options) {
  const next = Math.max(EYE_MIN, Math.min(EYE_MAX, value));
  if (!isFinite(next) || Math.abs(next - eyeHeight) < 0.0005) return;
  eyeHeight = next;
  applyEyeHeight();
  updateEyeUi();
  saveEyeHeightLater();
  if (!options || options.notice !== false) showEyeNotice();
}

// VR 안에서는 화면 위 버튼이 보이지 않으므로 지금 높이를 눈앞에 잠깐 띄워 준다.
function showEyeNotice() {
  eyeNoticeUntil = performance.now() + 1500;
  if (!renderer.xr.isPresenting) return;
  if (!eyeNotice) {
    const canvas = document.createElement('canvas');
    canvas.width = 512;
    canvas.height = 128;
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    const mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(0.44, 0.11),
      new THREE.MeshBasicMaterial({
        map: texture, transparent: true, toneMapped: false, depthTest: false,
      }),
    );
    mesh.position.set(0, -0.22, -0.9);
    mesh.renderOrder = 999;
    camera.add(mesh);
    eyeNotice = { mesh, canvas, texture };
  }
  const ctx = eyeNotice.canvas.getContext('2d');
  ctx.clearRect(0, 0, 512, 128);
  ctx.fillStyle = 'rgba(8, 15, 30, .82)';
  ctx.fillRect(0, 0, 512, 128);
  ctx.fillStyle = '#e2e8f0';
  ctx.font = 'bold 46px "Malgun Gothic", "Noto Sans KR", sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(`시점 높이 ${eyeHeight.toFixed(2)} m`, 256, 66);
  eyeNotice.texture.needsUpdate = true;
  eyeNotice.mesh.visible = true;
}

function calibrateVrFloor() {
  if (vrFloorChecked || !renderer.xr.isPresenting) return;
  vrFloorFrames += 1;
  if (vrFloorFrames < 20) return;          // 첫 몇 프레임은 머리 위치가 아직 들어오지 않는다.
  vrFloorChecked = true;
  const head = camera.position.y;          // VR에서는 기기가 알려준 머리 높이가 들어 있다.
  // 1.1m도 되지 않으면 바닥을 못 잡은 것으로 보고 기본 눈높이만큼 올려 준다.
  if (head < 1.1) vrFloorFix = EYE_HEIGHT - head;
  applyEyeHeight();
}

function setupEyeControls() {
  applyEyeHeight();
  updateEyeUi();
  if (!eyeBtn || !eyePanel) return;
  eyeBtn.addEventListener('click', () => {
    eyePanel.hidden = !eyePanel.hidden;
    eyeBtn.classList.toggle('on', !eyePanel.hidden);
  });
  if (eyeRange) {
    eyeRange.min = EYE_MIN.toFixed(2);
    eyeRange.max = EYE_MAX.toFixed(2);
    eyeRange.value = eyeHeight.toFixed(2);
    eyeRange.addEventListener('input', () => {
      setEyeHeight(parseFloat(eyeRange.value), { notice: false });
    });
  }
  eyePanel.querySelectorAll('[data-eye-preset]').forEach((button) => {
    button.addEventListener('click', () => {
      setEyeHeight(parseFloat(button.dataset.eyePreset), { notice: false });
    });
  });
}

/* ------------------------------------------------------------------ */
/* VR                                                                  */
/* ------------------------------------------------------------------ */

const controllers = [];
const vrSlot = document.getElementById('exVrSlot');

// 입장 화면의 VR 버튼에 지금 상태를 그대로 적는다.
// (VR이 안 되는 이유를 화면에서 바로 알 수 있어야 한다.)
function setVrSlot(label, icon, ready) {
  if (!vrSlot) return;
  vrSlot.hidden = false;
  vrSlot.disabled = !ready;
  vrSlot.innerHTML = `<i class="fa-solid ${icon}"></i> ${label}`;
  vrSlot.style.opacity = ready ? '' : '.6';
  vrSlot.style.cursor = ready ? '' : 'default';
}

async function enterVr() {
  setVrSlot('VR 준비 중…', 'fa-spinner', false);
  try {
    const session = await navigator.xr.requestSession('immersive-vr', {
      optionalFeatures: ['local-floor', 'bounded-floor', 'hand-tracking'],
    });
    await renderer.xr.setSession(session);
  } catch (error) {
    setVrSlot('VR을 시작하지 못했습니다', 'fa-triangle-exclamation', false);
  }
}

function setupVr() {
  // WebXR은 https(또는 localhost) 주소에서만 열린다. 사내 http 주소로 들어오면
  // navigator.xr 자체가 없어서 아무 일도 일어나지 않으므로 이유를 적어 준다.
  if (!window.isSecureContext) {
    setVrSlot('VR은 https 주소로 접속해야 합니다', 'fa-lock', false);
    return;
  }
  if (!navigator.xr || !navigator.xr.isSessionSupported) {
    setVrSlot('이 브라우저는 VR을 지원하지 않습니다', 'fa-vr-cardboard', false);
    return;
  }

  navigator.xr.isSessionSupported('immersive-vr').then((supported) => {
    if (!supported) {
      setVrSlot('연결된 VR 기기가 없습니다', 'fa-vr-cardboard', false);
      return;
    }
    setVrSlot('VR로 입장하기', 'fa-vr-cardboard', true);
    vrSlot.addEventListener('click', enterVr);
  }).catch(() => {
    setVrSlot('VR 상태를 확인할 수 없습니다', 'fa-vr-cardboard', false);
  });

  // 입장 버튼을 거치지 않고 VR로 바로 들어와도 걸어 다닐 수 있어야 한다.
  renderer.xr.addEventListener('sessionstart', () => {
    closeModal();
    enterHall();
    // 기기마다 바닥 기준이 달라서 들어올 때마다 다시 잰다.
    vrFloorFix = 0;
    vrFloorFrames = 0;
    vrFloorChecked = false;
    applyEyeHeight();
    // 왼손 컨트롤러가 잡히기 전까지는 시야 쪽에 붙여 두고, 잡히면 손목으로 옮긴다.
    const leftHand = controllers.find((item) => item.userData.handedness === 'left');
    dockVrMenu(leftHand || null);
  });
  renderer.xr.addEventListener('sessionend', () => {
    setVrSlot('VR로 입장하기', 'fa-vr-cardboard', true);
    if (eyeNotice) eyeNotice.mesh.visible = false;
    if (vrMenu) vrMenu.visible = false;
    applyEyeHeight();
  });

  for (let index = 0; index < 2; index += 1) {
    const controller = renderer.xr.getController(index);
    controller.addEventListener('selectstart', () => {
      const menuButton = controllerMenuTarget(controller);
      if (menuButton) { runVrMenu(menuButton.userData.vrMenu.action); return; }
      const artwork = controllerTarget(controller);
      if (artwork) openArtwork(artwork);
    });
    // 어느 쪽이 왼손인지는 기기가 연결될 때 알려 준다.
    controller.addEventListener('connected', (event) => {
      const handedness = event.data && event.data.handedness;
      controller.userData.handedness = handedness;
      if (handedness === 'left') dockVrMenu(controller);
    });
    controller.addEventListener('disconnected', () => {
      controller.userData.handedness = null;
      if (vrMenu && vrMenu.parent === controller) dockVrMenu(null);
    });
    const ray = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, 0, -1),
      ]),
      new THREE.LineBasicMaterial({ color: 0x60a5fa }),
    );
    ray.scale.z = 6;
    controller.add(ray);
    player.add(controller);
    controllers.push(controller);
  }
}

/* ------------------------------------------------------------------ */
/* VR 메뉴 (나가기)                                                      */
/* ------------------------------------------------------------------ */

// VR을 쓰는 동안에는 화면 위 HUD가 보이지 않아 [전시장 나가기] 버튼을 누를 수 없다.
// 그래서 같은 기능을 컨트롤러에 붙은 작은 메뉴판으로 만들어 준다.
// (왼손 컨트롤러 위에 뜨고, 반대쪽 컨트롤러로 겨눠 트리거를 누르면 실행된다.)
let vrMenu = null;
let vrMenuBusy = false;
const vrMenuTargets = [];

function drawVrMenuButton(mesh, hover) {
  const info = mesh.userData.vrMenu;
  const { canvas, texture } = info;
  const ctx = canvas.getContext('2d');
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (hover) ctx.fillStyle = '#2563eb';
  else ctx.fillStyle = info.primary ? 'rgba(37, 99, 235, .72)' : 'rgba(15, 23, 42, .84)';
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = 'rgba(255, 255, 255, .38)';
  ctx.lineWidth = 5;
  ctx.strokeRect(3, 3, w - 6, h - 6);
  ctx.fillStyle = '#ffffff';
  ctx.font = 'bold 50px "Malgun Gothic", "Noto Sans KR", sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(info.label, w / 2, h / 2 + 2);
  texture.needsUpdate = true;
  info.hover = hover;
}

function createVrMenuButton(label, action, primary) {
  const canvas = document.createElement('canvas');
  canvas.width = 512;
  canvas.height = 128;
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const mesh = new THREE.Mesh(
    new THREE.PlaneGeometry(0.26, 0.065),
    new THREE.MeshBasicMaterial({
      map: texture, transparent: true, toneMapped: false, depthTest: false,
    }),
  );
  mesh.renderOrder = 998;
  mesh.userData.vrMenu = { action, label, canvas, texture, primary, hover: false };
  drawVrMenuButton(mesh, false);
  vrMenuTargets.push(mesh);
  return mesh;
}

function buildVrMenu() {
  if (vrMenu) return vrMenu;
  vrMenu = new THREE.Group();
  const exit = createVrMenuButton('목록으로 나가기', 'exit', true);
  exit.position.y = 0.037;
  const endVr = createVrMenuButton('VR 종료', 'endvr', false);
  endVr.position.y = -0.037;
  vrMenu.add(exit, endVr);
  vrMenu.visible = false;
  return vrMenu;
}

// 왼손 컨트롤러가 있으면 손목 위에, 없으면 시야 왼쪽 아래에 띄운다.
function dockVrMenu(controller) {
  const menu = buildVrMenu();
  if (controller) {
    menu.position.set(0, 0.07, -0.04);
    menu.rotation.set(-Math.PI / 3, 0, 0);
    controller.add(menu);
  } else {
    menu.position.set(-0.22, -0.26, -0.70);
    menu.rotation.set(-0.34, 0.30, 0);
    camera.add(menu);
  }
  menu.visible = renderer.xr.isPresenting;
}

function runVrMenu(action) {
  if (vrMenuBusy) return;
  vrMenuBusy = true;
  const session = renderer.xr.getSession();
  // 기기가 끝내기를 늦게 알려 줘도 나가기가 멈추지 않도록 잠깐만 기다린다.
  const ended = session ? session.end().catch(() => {}) : Promise.resolve();
  const done = Promise.race([ended, new Promise((resolve) => setTimeout(resolve, 1200))]);
  done.then(() => {
    // '목록으로'는 VR을 먼저 닫고 나가야 헤드셋이 빈 화면에 머물지 않는다.
    if (action === 'exit' && exitLink && exitLink.href) window.location.href = exitLink.href;
  }).finally(() => { vrMenuBusy = false; });
}

const controllerMatrix = new THREE.Matrix4();

function controllerMenuTarget(controller) {
  if (!vrMenu || !vrMenu.visible) return null;
  if (vrMenu.parent === controller) return null;   // 메뉴가 달린 손으로는 겨눌 수 없다.
  controllerMatrix.identity().extractRotation(controller.matrixWorld);
  raycaster.ray.origin.setFromMatrixPosition(controller.matrixWorld);
  raycaster.ray.direction.set(0, 0, -1).applyMatrix4(controllerMatrix);
  const hit = raycaster.intersectObjects(vrMenuTargets, false)[0];
  return hit && hit.distance <= 5 ? hit.object : null;
}

function updateVrMenuHover() {
  if (!vrMenu || !vrMenu.visible) return;
  const hovered = new Set();
  controllers.forEach((controller) => {
    const hit = controllerMenuTarget(controller);
    if (hit) hovered.add(hit);
  });
  vrMenuTargets.forEach((mesh) => {
    const on = hovered.has(mesh);
    if (mesh.userData.vrMenu.hover !== on) drawVrMenuButton(mesh, on);
  });
}

function controllerTarget(controller) {
  controllerMatrix.identity().extractRotation(controller.matrixWorld);
  raycaster.ray.origin.setFromMatrixPosition(controller.matrixWorld);
  raycaster.ray.direction.set(0, 0, -1).applyMatrix4(controllerMatrix);
  const hit = raycaster.intersectObjects(pickTargets, false)[0];
  return hit && hit.distance <= REACH ? hit.object.userData.artwork : null;
}

function readXrSticks() {
  // 조작스틱: 왼쪽은 이동, 오른쪽은 좌우 회전
  const session = renderer.xr.getSession();
  if (!session) return false;
  let used = false;
  for (const source of session.inputSources) {
    const pad = source.gamepad;
    if (!pad || !pad.axes || pad.axes.length < 4) continue;
    const x = pad.axes[2] || pad.axes[0] || 0;
    const y = pad.axes[3] || pad.axes[1] || 0;
    if (source.handedness === 'right') {
      if (Math.abs(x) > 0.15) { turnInput = -x; used = true; }
      // 위로 밀면 시점이 올라간다(스틱 y는 위가 음수).
      if (Math.abs(y) > 0.6) { heightInput = -y; used = true; }
    } else {
      if (Math.abs(x) > 0.15 || Math.abs(y) > 0.15) {
        move.x = x;
        move.y = -y;
        used = true;
      }
    }
  }
  return used;
}

/* ------------------------------------------------------------------ */
/* 렌더 루프                                                            */
/* ------------------------------------------------------------------ */

const clock = new THREE.Clock();
const forwardVector = new THREE.Vector3();
const rightVector = new THREE.Vector3();
const cameraWorld = new THREE.Vector3();
let entered = false;
let sinceVideoCheck = 0;

function updatePlayer(delta) {
  const axis = keyAxis();
  const inXr = renderer.xr.isPresenting;
  if (inXr) {
    move.x = 0;
    move.y = 0;
    turnInput = 0;
    heightInput = 0;
    readXrSticks();
    if (heightInput) setEyeHeight(eyeHeight + heightInput * 0.6 * delta);
  }

  const turn = axis.turn + turnInput;
  if (turn) player.rotation.y += turn * TURN_SPEED * delta;

  let forward = axis.forward + move.y;
  let strafe = axis.strafe + move.x;
  const length = Math.hypot(forward, strafe);
  if (length > 1) { forward /= length; strafe /= length; }
  if (!forward && !strafe) return;

  // 이동 방향은 지금 바라보는 쪽(VR에서는 머리 방향)을 따른다.
  camera.getWorldDirection(forwardVector);
  forwardVector.y = 0;
  if (forwardVector.lengthSq() < 0.0001) forwardVector.set(0, 0, -1);
  forwardVector.normalize();
  rightVector.set(forwardVector.z, 0, -forwardVector.x);

  const speed = BASE_SPEED * (HALL.moveSpeed || 1) * (running ? 1.75 : 1) * delta;
  const next = {
    x: player.position.x + (forwardVector.x * forward + rightVector.x * -strafe) * speed,
    z: player.position.z + (forwardVector.z * forward + rightVector.z * -strafe) * speed,
  };
  clampPosition(next);
  player.position.x = next.x;
  player.position.z = next.z;
}

function updateVideos(delta) {
  sinceVideoCheck += delta;
  if (sinceVideoCheck < 0.5) return;
  sinceVideoCheck = 0;

  camera.getWorldPosition(cameraWorld);
  const videos = artworks
    .filter((artwork) => artwork.video && !artwork.userPlaying)
    .map((artwork) => ({
      artwork,
      distance: artwork.group.position.distanceTo(cameraWorld),
    }))
    .sort((a, b) => a.distance - b.distance);

  videos.forEach((entry, index) => {
    const shouldPlay = index < MAX_PREVIEW_VIDEOS
      && entry.distance < VIDEO_PREVIEW_DISTANCE
      && !modal.classList.contains('open');
    const video = entry.artwork.video;
    if (shouldPlay && video.paused) {
      video.play().catch(() => {});
    } else if (!shouldPlay && !video.paused) {
      video.pause();
    }
  });
}

function updateSlideshow(now) {
  if (!HALL.autoSlide) return;
  const interval = Math.max(HALL.slideSeconds, 2) * 1000;
  artworks.forEach((artwork) => {
    const list = artwork.slot.media;
    if (list.length < 2 || list.some((item) => item.kind === 'video')) return;
    if (modalArtwork === artwork) return;
    if (!artwork.lastSlideAt) { artwork.lastSlideAt = now; return; }
    if (now - artwork.lastSlideAt < interval) return;
    artwork.lastSlideAt = now;
    showMedia(artwork, (artwork.index + 1) % list.length);
  });
}

function updateReticle() {
  if (renderer.xr.isPresenting || modal.classList.contains('open')) {
    reticle.classList.remove('active');
    return;
  }
  const artwork = centreTarget();
  reticle.classList.toggle('active', Boolean(artwork));
  if (artwork) {
    const slot = artwork.slot;
    const kind = slot.media[0].kind === 'video'
      ? (artwork.userPlaying ? '동영상 멈춤' : '동영상 재생')
      : `사진 ${slot.media.length}장`;
    focusLabel.textContent = `${slot.title || slot.name} · 클릭하면 ${kind}`;
  } else {
    focusLabel.textContent = '작품 가까이 다가가 클릭하면 크게 볼 수 있습니다';
  }
}

renderer.setAnimationLoop(() => {
  const delta = Math.min(clock.getDelta(), 0.1);
  if (entered) {
    calibrateVrFloor();
    updatePlayer(delta);
    updateVideos(delta);
    updateSlideshow(performance.now());
    updateReticle();
    updateVrMenuHover();
    // 눈앞 안내판은 VR 안에서만 쓴다(화면에서는 HUD 패널이 보여 준다).
    if (eyeNotice) {
      eyeNotice.mesh.visible = renderer.xr.isPresenting && performance.now() < eyeNoticeUntil;
    }
  }
  renderer.render(scene, camera);
});

window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

/* ------------------------------------------------------------------ */
/* 입장                                                                */
/* ------------------------------------------------------------------ */

fullBtn.addEventListener('click', () => {
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  } else {
    stage.requestFullscreen?.().catch(() => {});
  }
});

function enterHall() {
  if (entered) return;
  entered = true;
  gate.hidden = true;
  hud.hidden = false;
  hint.hidden = false;
  if (isTouchDevice) {
    touchPad.hidden = false;
    hint.textContent = '왼쪽 조이스틱으로 이동 · 화면을 쓸어 둘러보기 · 작품 터치';
  }
  if (HALL.bgmEnabled) startBgm(); else bgmBtn.classList.add('off');
  setTimeout(() => { hint.style.opacity = '0'; }, 7000);
}

enterBtn.addEventListener('click', enterHall);

(async function boot() {
  setupBgm();
  setupEyeControls();
  setupVr();
  try {
    await Promise.all([prepareArtworks(), environmentReady]);
  } catch (error) {
    loadingText.textContent = '작품을 불러오는 중 문제가 발생했습니다.';
  }
  loading.hidden = true;
  gate.hidden = false;
  if (!artworks.length) {
    focusLabel.textContent = '아직 올린 전시파일이 없습니다. [전시파일 셋팅]에서 작품을 올려 주세요.';
  }
})();
