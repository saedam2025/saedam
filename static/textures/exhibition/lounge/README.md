# Lounge photographic materials

Photographed texture maps and HDR sky from [Poly Haven](https://polyhaven.com),
released under [CC0](https://polyhaven.com/license). Local copies allow viewing
without third-party network access. Original download URLs and MD5 checksums are
recorded in `sources.json`.

| Local asset | Source | Use |
| --- | --- | --- |
| wood_floor_* | https://polyhaven.com/a/wood_floor | Oak floor, wood furniture and frames; walnut uses a darker tint |
| plastered_wall_* | https://polyhaven.com/a/plastered_wall | Walls and ceiling |
| concrete_floor_02_* | https://polyhaven.com/a/concrete_floor_02 | Concrete option and planters |
| stone_tiles_02_* | https://polyhaven.com/a/stone_tiles_02 | Limestone / stone option |
| daylight_1k.hdr | https://polyhaven.com/a/kloofendal_48d_partly_cloudy_puresky | Visible sky and environment reflections |

PBR maps are the original 1K JPEGs: Diffuse uses sRGB, OpenGL normal and roughness
use linear data. The sky is the 1K HDR, which is ample seen through a skylight and
keeps the first load small. The existing `plain` option stays
untextured. The lounge and classroom builders use these materials. Classroom
furniture crops a single wood plank to avoid displaying flooring seams on desks.

`RGBELoader.js` comes from Three.js r161 (matching the bundled renderer), under
MIT; its only change is the import path to the local `three.module.js`.
The license is in `static/js/vendor/three/LICENSE.txt`.
