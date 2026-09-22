import * as THREE from './vendor/three/three.module.js';
import { RGBELoader } from './vendor/three/RGBELoader.js';

// Photographed CC0 assets are bundled locally. See each folder's sources.json.
const ROOT = new URL('../textures/exhibition/lounge/', import.meta.url);
const STREET = new URL('../textures/exhibition/street/', import.meta.url);
const SURFACES = {
  oak: { asset: 'wood_floor', size: 1.7, normal: 0.32, wood: true, gain: 1.22 },
  walnut: { asset: 'wood_floor', size: 1.7, normal: 0.32, wood: true },
  plaster: { asset: 'plastered_wall', size: 2, normal: 0.18, gain: 1.95 },
  concrete: { asset: 'concrete_floor_02', size: 2, normal: 0.4 },
  limestone: { asset: 'stone_tiles_02', size: 2, normal: 0.28 },
  // 길가(전시관7)에서만 쓰는 사진 재질. 한 장에 담긴 실제 크기가 size(m)다.
  asphalt: { root: STREET, asset: 'asphalt', size: 3.6, normal: 0.7, rough: 0.92 },
  paving: { root: STREET, asset: 'paving', size: 2.4, normal: 0.6, rough: 0.88 },
  streetwall: { root: STREET, asset: 'street_wall', size: 2.6, normal: 0.55, rough: 0.94 },
  shutter: { root: STREET, asset: 'metal_shutter', size: 2.2, normal: 0.65, rough: 0.52, metal: 0.35 },
};

export function photographicMaterials(renderer, fallback) {
  const loader = new THREE.TextureLoader();
  const sources = new Map();
  const pending = [];
  function source(asset, root) {
    if (!sources.has(asset)) {
      sources.set(asset, Promise.all(['Diffuse', 'nor_gl', 'Rough'].map(async channel => {
        const texture = await loader.loadAsync(new URL(`${asset}_${channel}.jpg`, root).href);
        texture.colorSpace = channel === 'Diffuse' ? THREE.SRGBColorSpace : THREE.NoColorSpace;
        texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
        // 8배 비등방 필터는 넓은 바닥을 비스듬히 볼 때 픽셀마다 표본을 8번 뽑는다.
        // 4배로도 나뭇결이 살아 있고, 고해상도 화면에서 프레임이 훨씬 안정된다.
        texture.anisotropy = Math.min(4, renderer.capabilities.getMaxAnisotropy());
        return texture;
      })));
    }
    return sources.get(asset);
  }
  const make = (kind, color, width = 1, height = 1) => {
    const spec = SURFACES[kind];
    if (!spec) return fallback(kind, color, width, height);
    const material = new THREE.MeshStandardMaterial({
      color, roughness: 0.85, metalness: spec.metal || 0, envMapIntensity: 0.5,
    });
    const ready = source(spec.asset, spec.root || ROOT).then(textures => {
      [material.map, material.normalMap, material.roughnessMap] = textures.map(texture => {
        const map = texture.clone();
        map.repeat.set(width / spec.size, height / spec.size);
        return map;
      });
      // The photograph already contains the wood's colour; keep the saved tint subtle.
      if (spec.wood) material.color.lerp(new THREE.Color(0xffffff), kind === 'walnut' ? 0.22 : 0.72);
      // The plaster was shot mid-grey; lift it so painted walls read as white
      // while the photograph's texture and the saved wall colour both survive.
      if (spec.gain) material.color.multiplyScalar(spec.gain);
      material.normalScale.setScalar(spec.normal);
      material.roughness = spec.rough || (spec.wood ? 0.76 : 0.95);
      material.needsUpdate = true;
    }).catch(error => {
      // A missing image must never keep visitors stuck at the entrance.
      console.warn(`Exhibition material unavailable: ${spec.asset}`, error);
    });
    pending.push(ready);
    return material;
  };
  return { make, ready: () => Promise.all(pending) };
}

export async function photographicSky(scene, renderer) {
  try {
    const texture = await new RGBELoader().loadAsync(new URL('daylight_1k.hdr', ROOT).href);
    texture.mapping = THREE.EquirectangularReflectionMapping;
    scene.background = texture;
    scene.backgroundIntensity = 0.8;
    const pmrem = new THREE.PMREMGenerator(renderer);
    try {
      const environment = pmrem.fromEquirectangular(texture);
      scene.environment = environment.texture;
      // Retain the render target for explicit scene teardown if the viewer becomes an SPA.
      scene.userData.loungeEnvironment = environment;
    } finally {
      pmrem.dispose();
    }
  } catch (error) {
    console.warn('Exhibition sky unavailable; using daylight background.', error);
  }
}

// 길가 전시관처럼 하늘이 통째로 보이는 곳: 사진 하늘을 돔에 두르고,
// 같은 하늘의 HDR로 빛과 반사를 만든다.
//
// 돔을 쓰는 이유. scene.background 에 파노라마를 걸면 three.js 가 이를 큐브맵
// 여섯 장으로 다시 구워, 4K 한 장(33MB)이 6×2048²(100MB)로 불어난다. 돔은
// 사진 한 장을 그대로 쓴다. 대신 구(球)의 가로 UV는 파노라마 규약과 좌우가
// 반대라, 사진을 미리 뒤집어 저장해 두었다(textures/exhibition/street/README.md).
//
// 하늘 사진은 이미 톤매핑된 8비트 사진이므로 toneMapped=false 로 그대로 보여 준다.
// 반대로 빛으로 쓰는 sky_env.hdr 는 밝기 정보가 살아 있는 HDR이다. 두 파일 모두
// 해가 atan2(z, x) = -117°, 고도 48° 에 오도록 같은 각도만큼 돌려 두었으므로,
// 그 방향에 둔 태양광이 만드는 그림자가 눈에 보이는 해의 위치와 맞는다.
export function photographicStreetSky(scene, renderer, lowPower) {
  const dome = new THREE.Mesh(
    new THREE.SphereGeometry(200, 48, 28),
    // 사진이 도착하기 전에도 하늘색이 보이도록 색을 먼저 칠해 둔다.
    new THREE.MeshBasicMaterial({
      color: 0x9fb4cf, side: THREE.BackSide, fog: false, toneMapped: false, depthWrite: false,
    }),
  );
  dome.name = 'street-sky';
  dome.renderOrder = -1;
  scene.add(dome);

  const sky = new THREE.TextureLoader()
    .loadAsync(new URL(lowPower ? 'sky_2k.jpg' : 'sky_4k.jpg', STREET).href)
    .then(texture => {
      texture.colorSpace = THREE.SRGBColorSpace;
      texture.anisotropy = Math.min(4, renderer.capabilities.getMaxAnisotropy());
      dome.material.map = texture;
      dome.material.color.setHex(0xffffff);
      dome.material.needsUpdate = true;
    })
    .catch(error => {
      // 사진이 없어도 색칠한 하늘 아래에서 관람은 이어진다.
      console.warn('Exhibition sky photograph unavailable.', error);
    });

  const light = new RGBELoader().loadAsync(new URL('sky_env.hdr', STREET).href)
    .then(texture => {
      texture.mapping = THREE.EquirectangularReflectionMapping;
      const pmrem = new THREE.PMREMGenerator(renderer);
      try {
        const environment = pmrem.fromEquirectangular(texture);
        scene.environment = environment.texture;
        // Retain the render target for explicit scene teardown if the viewer becomes an SPA.
        scene.userData.streetEnvironment = environment;
      } finally {
        pmrem.dispose();
        texture.dispose();
      }
    })
    .catch(error => {
      console.warn('Exhibition sky lighting unavailable.', error);
    });

  return { dome, ready: Promise.all([sky, light]) };
}

// 담벼락·셔터에 붙이는 그라피티 한 장(4×4 칸에 16점이 들어 있다).
// 사진이 도착한 뒤에 복제해야 복제본까지 GPU에 올라가므로, 읽고 나서 넘겨준다.
export async function graffitiSheet() {
  const texture = await new THREE.TextureLoader()
    .loadAsync(new URL('graffiti.webp', STREET).href);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

export function glazedSkylight(scene, box, metal, trim, width, depth, height, lowPower) {
  // 유리를 '투과(transmission)' 재질로 두면 three.js가 유리 뒤를 보여 주기 위해
  // 매 프레임 장면을 한 번 더 그린다(transmission pass). 천창은 하늘만 비치므로
  // 옅은 투명 + 환경 반사로 바꿨다. 보이는 모습은 그대로이면서 장면을 두 번 그리는 일이 없어진다.
  const glass = new THREE.MeshPhysicalMaterial({
    color: 0xeaf7f6, metalness: 0, roughness: 0.055,
    transparent: true, opacity: lowPower ? 0.1 : 0.16,
    side: THREE.DoubleSide, depthWrite: false, envMapIntensity: 0.5,
  });
  const columns = 3, rows = Math.ceil(depth / 2.7);
  const cellW = width / columns, cellD = depth / rows;
  // 칸마다 유리판을 두면 유리 한 장에 draw call이 15번 든다.
  // 칸을 나누는 창틀이 유리 아래에 따로 있으므로 유리는 한 장으로 덮는다.
  const pane = new THREE.Mesh(new THREE.PlaneGeometry(width, depth), glass);
  pane.name = 'lounge-skylight-glass';
  pane.rotation.x = -Math.PI / 2;
  pane.position.set(0, height + 0.32, 0);
  // Transparent glazing must not cast an opaque rectangle over the room.
  pane.castShadow = false;
  pane.receiveShadow = false;
  scene.add(pane);
  for (let x = 0; x <= columns; x++) box(0.07, 0.16, depth + 0.12, metal, -width / 2 + x * cellW, height + 0.26, 0);
  for (let z = 0; z <= rows; z++) box(width, 0.16, 0.07, metal, 0, height + 0.26, -depth / 2 + z * cellD);
  for (const sign of [-1, 1]) {
    box(width + 0.3, 0.35, 0.16, trim, 0, height + 0.1, sign * (depth / 2 + 0.08));
  }
}
