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

const modal = document.getElementById('exModal');
const modalStage = document.getElementById('exModalStage');
const modalCaption = document.getElementById('exModalCaption');
const modalClose = document.getElementById('exModalClose');
const modalPrev = document.getElementById('exModalPrev');
const modalNext = document.getElementById('exModalNext');

const HALL = SCENE.hall;
const LAYOUT = SCENE.layout;
const SLOTS = SCENE.slots.filter((slot) => slot.media && slot.media.length);

const EYE_HEIGHT = 1.62;
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

const camera = new THREE.PerspectiveCamera(62, window.innerWidth / window.innerHeight, 0.1, 220);
camera.position.set(0, EYE_HEIGHT, 0);

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
const { blockers, frameMaterial } = buildEnvironment(
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

  const frame = createFrame(size.width, size.height, frameMaterial);
  frame.userData.sharedMaterial = frameMaterial;
  group.add(frame);
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
    videoPlaying: false,
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

  // 동영상 자리에는 재생 표시를 함께 띄운다.
  if (first.kind === 'video') {
    const badge = new THREE.Mesh(
      new THREE.CircleGeometry(0.19, 32),
      new THREE.MeshBasicMaterial({ color: 0x0f172a, transparent: true, opacity: 0.72 }),
    );
    badge.position.set(0, 0, 0.02);
    group.add(badge);
    artwork.badge = badge;
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
    if (artwork.badge) artwork.badge.visible = true;
    if (artwork.video.videoWidth) {
      refit(artwork, artwork.video.videoWidth / artwork.video.videoHeight);
    }
    return;
  }

  if (artwork.badge) artwork.badge.visible = false;
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

window.addEventListener('keydown', (event) => {
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

function openArtwork(artwork) {
  if (!artwork) return;
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
  });
  renderer.xr.addEventListener('sessionend', () => {
    setVrSlot('VR로 입장하기', 'fa-vr-cardboard', true);
  });

  for (let index = 0; index < 2; index += 1) {
    const controller = renderer.xr.getController(index);
    controller.addEventListener('selectstart', () => {
      const artwork = controllerTarget(controller);
      if (artwork) openArtwork(artwork);
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

const controllerMatrix = new THREE.Matrix4();

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
    readXrSticks();
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
    .filter((artwork) => artwork.video)
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
    const kind = slot.media[0].kind === 'video' ? '동영상 재생' : `사진 ${slot.media.length}장`;
    focusLabel.textContent = `${slot.title || slot.name} · 클릭하면 ${kind}`;
  } else {
    focusLabel.textContent = '작품 가까이 다가가 클릭하면 크게 볼 수 있습니다';
  }
}

renderer.setAnimationLoop(() => {
  const delta = Math.min(clock.getDelta(), 0.1);
  if (entered) {
    updatePlayer(delta);
    updateVideos(delta);
    updateSlideshow(performance.now());
    updateReticle();
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
  setupVr();
  try {
    await prepareArtworks();
  } catch (error) {
    loadingText.textContent = '작품을 불러오는 중 문제가 발생했습니다.';
  }
  loading.hidden = true;
  gate.hidden = false;
  if (!artworks.length) {
    focusLabel.textContent = '아직 올린 전시파일이 없습니다. [전시파일 셋팅]에서 작품을 올려 주세요.';
  }
})();
