# Street photographic materials (전시관7 · 버스정류장형)

Photographed texture maps, sky and graffiti from [Poly Haven](https://polyhaven.com)
and [ambientCG](https://ambientcg.com), both released under
[CC0](https://creativecommons.org/publicdomain/zero/1.0/). Local copies allow
viewing without third-party network access. Download URLs, the MD5 of each
original and the transform applied to it are recorded in `sources.json`.

| Local asset | Source | Use |
| --- | --- | --- |
| asphalt_* | https://polyhaven.com/a/asphalt_02 | 차도 |
| paving_* | https://polyhaven.com/a/patterned_concrete_pavers | 인도 보도블록 |
| street_wall_* | https://polyhaven.com/a/painted_worn_brick | 가게 건물과 건너편 건물 외벽 |
| metal_shutter_* | https://polyhaven.com/a/painted_metal_shutter | 셔터 내린 가게 |
| sky_4k.jpg · sky_2k.jpg | https://polyhaven.com/a/kloofendal_48d_partly_cloudy_puresky | 눈에 보이는 하늘(돔에 두른다) |
| sky_env.hdr | 같은 하늘 (`../lounge/daylight_1k.hdr`) | 빛과 반사(PMREM) |
| graffiti.webp | https://ambientcg.com/view?id=GraffitiSet001 | 담벼락·셔터의 그라피티 16점(4×4) |

PBR maps follow the lounge convention: `Diffuse` is sRGB, `nor_gl`(OpenGL normal)
and `Rough` are linear. Diffuse is kept at 1K and the two linear channels at 512
— enough at street distances, and it keeps the hall's first load near 2 MB.

## 하늘 두 장을 같은 각도로 돌려 둔 이유

`sky_*.jpg`는 눈에 보이는 하늘이고 `sky_env.hdr`는 빛으로 쓰는 하늘이다.
전시관 코드의 태양은 `atan2(z, x) = -117°`, 고도 48°에 있으므로, 두 파일 모두
원본의 해(고도 48°)가 그 자리에 오도록 같은 각도만큼 가로로 굴려 두었다.
그래서 바닥에 지는 그림자의 방향이 하늘에 보이는 해와 어긋나지 않는다.

`sky_*.jpg`는 한 번 더 좌우로 뒤집혀 있다. 하늘을 구(球) 안쪽에 두르면 three.js
`SphereGeometry`의 가로 UV가 파노라마 규약과 좌우 반대이기 때문이다. 파노라마를
`scene.background`에 바로 걸면 three.js가 큐브맵 여섯 장으로 다시 구워
4K 한 장(33 MB)이 100 MB로 불어나므로, 돔에 사진 한 장을 그대로 둘렀다.

`sky_env.hdr`는 512×256으로 줄인 평문(RLE 없는) RGBE다. `PMREMGenerator`가
어차피 256픽셀 큐브맵으로 굽기 때문에 이보다 크면 파일만 커진다.
