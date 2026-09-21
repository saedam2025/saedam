(function (global) {
    const uiStyles = ['glass', 'solid', 'glow', 'neumorphism', 'clay'];
    const paletteNames = ['파스텔', '비비드', '뮤트', '하이콘트라스트', '딥틴트'];
    const effectsList = [
        'stars', 'bubbles', 'blobs', 'orbs', 'waves', 'geometry', 'rings', 'polygons', 'lines', 'starPop', 'starSprinkle', 'glitterField', 'cometTwinkle', 'neonGeometryFlow',
        'snowfall', 'fireflies', 'aurora', 'confetti', 'lanterns', 'rippleWaves', 'meteorShower', 'crystalPrisms', 'cosmicDust', 'floatingHearts', 'cyberLines', 'matrixDrops', 'autumnLeaves', 'circuitPaths', 'electricSparks', 'diamondPulse', 'lightPillars', 'magicRunes', 'plasmaOrbs', 'stardustTrails'
    ];
    const adjectives = ['미스틱', '네온', '사이버', '오가닉', '코스믹', '딥', '퓨어', '루미너스', '크리스탈', '벨벳', '아스트랄', '스파클링', '글로우', '매직', '아우라', '일루전', '드림', '일렉트릭', '팬텀', '인피니티', '루나', '솔라', '시크릿', '로얄', '엔젤릭', '루비', '사파이어', '에메랄드', '스텔라', '다이내믹', '프리즘', '에테르', '소닉', '루시드', '비비드', '폴리곤', '매트릭스', '헥사곤', '지오메트릭', '라인'];
    const nouns = ['오션', '블랙홀', '네뷸라', '선셋', '웨이브', '포레스트', '플레어', '갤럭시', '마블', '실버', '골드', '스톰', '이클립스', '클라우드', '라군', '펄', '스타더스트', '메트릭스', '퀘이사', '스페이스', '드롭', '플로우', '오로라', '블리즈', '스카이', '크리스탈', '오아시스', '유니버스', '홀로그램', '미라지', '오디세이', '스펙트럼', '호라이즌', '에코', '포털'];

    // 테마관리 화면의 고정 번호 체계:
    // 0 기본, 1 e리플렛, 2~2001 기본 갤러리, 2002~2501 포인트 컬러,
    // 2502~3001 딥 컬러, 3002~ 계절·기념일, 4001~ 시스템 테마.
    const themeCatalogDisplayOffsets = Object.freeze({
        gallery: 1,
        accent: 2001,
        deepColor: 2501,
        'deep-color': 2501,
        seasonal: 3001,
        system: 4000
    });

    function getThemeDisplayNumber(theme, fallbackIndex = 0) {
        if (!theme) return fallbackIndex;
        if (theme.isDefault || theme.key === 'default:0') return 0;
        if (theme.key === 'gallery:eleaflet') return 1;

        const catalogIndex = Number(theme.catalogIndex);
        const offset = themeCatalogDisplayOffsets[theme.catalog];
        if (Number.isInteger(catalogIndex) && catalogIndex > 0 && Number.isInteger(offset)) {
            return offset + catalogIndex;
        }
        return fallbackIndex;
    }

    function seededRandom(seedText) {
        let seed = 2166136261;
        for (let i = 0; i < seedText.length; i++) {
            seed ^= seedText.charCodeAt(i);
            seed = Math.imul(seed, 16777619);
        }
        return function () {
            seed += 0x6D2B79F5;
            let t = seed;
            t = Math.imul(t ^ (t >>> 15), t | 1);
            t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
            return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        };
    }

    function pick(list, rng) {
        return list[Math.floor(rng() * list.length)];
    }

    function ensureVisibleThemeLines(vars, primary) {
        const isInvisible = (value) => {
            const normalized = String(value || '').replace(/\s+/g, '').toLowerCase();
            return !normalized
                || normalized === 'transparent'
                || /^(rgba|hsla)\([^)]*,0(?:\.0+)?\)$/.test(normalized)
                || /^#[0-9a-f]{6}00$/.test(normalized);
        };
        const lineColor = `color-mix(in srgb, ${primary} 38%, #cbd5e1)`;
        if (isInvisible(vars['--card-border'])) vars['--card-border'] = lineColor;
        if (isInvisible(vars['--widget-border'])) vars['--widget-border'] = lineColor;
        vars['--theme-line-color'] = vars['--widget-border'] || vars['--card-border'] || lineColor;
        return vars;
    }

    function createTheme(index, catalog) {
        const rng = seededRandom(`saedam-theme:${catalog}:${index}`);
        const hue = Math.floor((index * 137.5) % 360);
        const paletteStyle = index % 5;
        const useComplementary = index % 7 === 0;
        const pointHue = useComplementary ? (hue + 180) % 360 : hue;
        const effectType = effectsList[index % effectsList.length];
        const uiStyle = uiStyles[index % uiStyles.length];

        let styleNameKOR = '유리';
        if (uiStyle === 'solid') styleNameKOR = '솔리드';
        else if (uiStyle === 'glow') styleNameKOR = '네온';
        else if (uiStyle === 'neumorphism') styleNameKOR = '뉴모피즘';
        else if (uiStyle === 'clay') styleNameKOR = '클레이';

        let bgS, bgL, priS, priL, effS, effL;
        switch (paletteStyle) {
            case 0: bgS = 40; bgL = 95; priS = 70; priL = 50; effS = 70; effL = 75; break;
            case 1: bgS = 70; bgL = 90; priS = 90; priL = 45; effS = 90; effL = 65; break;
            case 2: bgS = 20; bgL = 92; priS = 40; priL = 45; effS = 40; effL = 70; break;
            case 3: bgS = 10; bgL = 98; priS = 85; priL = 30; effS = 85; effL = 60; break;
            default: bgS = 50; bgL = 85; priS = 65; priL = 40; effS = 65; effL = 65; break;
        }

        const pName = paletteNames[paletteStyle];
        const name = `[${styleNameKOR}/${pName}] ${pick(adjectives, rng)} ${pick(nouns, rng)}`;
        const primary = `hsl(${pointHue}, ${priS}%, ${priL}%)`;
        const primaryDark = `hsl(${pointHue}, ${priS}%, ${Math.max(20, priL - 10)}%)`;
        const bg = `linear-gradient(-45deg, hsl(${hue}, ${bgS}%, ${bgL}%), hsl(${(hue + 30) % 360}, ${bgS + 10}%, ${bgL - 5}%), hsl(${(hue - 30 + 360) % 360}, ${bgS}%, ${bgL}%))`;

        const vars = {
            '--body-bg': bg,
            '--primary-color': primary,
            '--primary-dark': primaryDark,
            '--text-dark': '#0f172a',
            '--text-gray': '#64748b',
            '--input-text': '#1e293b',
            '--tooltip-bg': 'rgba(15, 23, 42, 0.95)',
            '--tooltip-text': '#ffffff',
            '--effect-color1': `hsl(${hue}, ${effS}%, ${effL}%)`,
            '--effect-color2': `hsl(${(hue + 40) % 360}, ${effS}%, ${effL}%)`,
            '--effect-color3': `hsl(${(hue - 40 + 360) % 360}, ${effS}%, ${effL}%)`
        };

        if (uiStyle === 'glass') {
            vars['--card-bg'] = 'rgba(255, 255, 255, 0.6)';
            vars['--card-border'] = 'rgba(255, 255, 255, 0.9)';
            vars['--card-backdrop'] = 'blur(24px)';
            vars['--card-shadow'] = '0 15px 40px rgba(0, 0, 0, 0.1)';
            vars['--input-bg'] = 'rgba(255, 255, 255, 0.8)';
            vars['--widget-bg'] = 'rgba(255, 255, 255, 0.7)';
            vars['--widget-hover'] = 'rgba(255, 255, 255, 1)';
            vars['--widget-border'] = 'rgba(255, 255, 255, 1)';
        } else if (uiStyle === 'solid') {
            vars['--card-bg'] = '#ffffff';
            vars['--card-border'] = 'transparent';
            vars['--card-backdrop'] = 'none';
            vars['--card-shadow'] = '0 20px 40px rgba(0, 0, 0, 0.1)';
            vars['--input-bg'] = '#f1f5f9';
            vars['--widget-bg'] = '#f1f5f9';
            vars['--widget-hover'] = '#e2e8f0';
            vars['--widget-border'] = 'transparent';
        } else if (uiStyle === 'glow') {
            vars['--card-bg'] = 'rgba(255, 255, 255, 0.8)';
            vars['--card-border'] = primary;
            vars['--card-backdrop'] = 'blur(10px)';
            vars['--card-shadow'] = `0 0 25px ${primary}`;
            vars['--input-bg'] = 'rgba(255,255,255,0.5)';
            vars['--widget-bg'] = 'rgba(255,255,255,0.5)';
            vars['--widget-hover'] = 'rgba(255,255,255,0.9)';
            vars['--widget-border'] = primary;
        } else if (uiStyle === 'neumorphism') {
            const neuBg = `hsl(${hue}, ${Math.max(0, bgS - 20)}%, 90%)`;
            const darkShadow = `hsl(${hue}, ${Math.max(0, bgS - 20)}%, 80%)`;
            vars['--body-bg'] = neuBg;
            vars['--card-bg'] = neuBg;
            vars['--card-border'] = 'transparent';
            vars['--card-backdrop'] = 'none';
            vars['--card-shadow'] = `12px 12px 24px ${darkShadow}, -12px -12px 24px #ffffff`;
            vars['--input-bg'] = 'rgba(255,255,255,0.45)';
            vars['--widget-bg'] = neuBg;
            vars['--widget-hover'] = neuBg;
            vars['--widget-border'] = 'transparent';
        } else if (uiStyle === 'clay') {
            const clayBg = `hsl(${hue}, ${bgS}%, 95%)`;
            vars['--card-bg'] = clayBg;
            vars['--card-border'] = 'transparent';
            vars['--card-backdrop'] = 'none';
            vars['--card-shadow'] = '10px 10px 20px rgba(0,0,0,0.1), inset -6px -6px 16px rgba(0,0,0,0.08), inset 6px 6px 16px rgba(255,255,255,0.9)';
            vars['--input-bg'] = 'rgba(255,255,255,0.55)';
            vars['--widget-bg'] = clayBg;
            vars['--widget-hover'] = clayBg;
            vars['--widget-border'] = 'transparent';
        }

        ensureVisibleThemeLines(vars, primary);

        return {
            name,
            type: effectType,
            vars,
            catalog,
            catalogIndex: index + 1
        };
    }

    function createThemeCatalog(catalog, count) {
        const themes = [];
        for (let i = 0; i < count; i++) {
            themes.push(createTheme(i, catalog));
        }
        return themes;
    }

    const accentPaletteNames = ['레몬 네온', '선플라워', '앰버', '탠저린', '실버 그레이', '그래파이트'];
    const accentPalettes = [
        { name: '레몬 네온', hue: 56, bgS: 92, bgL: 94, priS: 95, priL: 44, effS: 95, effL: 64 },
        { name: '선플라워', hue: 47, bgS: 86, bgL: 93, priS: 92, priL: 43, effS: 88, effL: 67 },
        { name: '앰버', hue: 36, bgS: 88, bgL: 92, priS: 92, priL: 45, effS: 90, effL: 64 },
        { name: '탠저린', hue: 25, bgS: 84, bgL: 93, priS: 90, priL: 50, effS: 90, effL: 66 },
        { name: '실버 그레이', hue: 215, bgS: 14, bgL: 94, priS: 18, priL: 46, effS: 22, effL: 68 },
        { name: '그래파이트', hue: 220, bgS: 10, bgL: 88, priS: 15, priL: 36, effS: 18, effL: 58 }
    ];
    const accentAdjectives = ['네온', '샤인', '글로우', '솔라', '앰버', '시트러스', '스파크', '크롬', '실버', '스톤', '모노', '그래파이트'];
    const accentNouns = ['레몬', '선빔', '라이트', '버튼', '패널', '오렌지', '캔들', '메탈', '그레이', '슬레이트', '포그', '코어'];

    function createAccentTheme(index) {
        const rng = seededRandom(`saedam-theme:accent:${index}`);
        const palette = accentPalettes[index % accentPalettes.length];
        const hueShift = Math.floor(index / accentPalettes.length) % 9 - 4;
        const hue = (palette.hue + hueShift + 360) % 360;
        const uiStyle = uiStyles[index % uiStyles.length];
        const effectType = effectsList[index % effectsList.length];

        let styleNameKOR = '유리';
        if (uiStyle === 'solid') styleNameKOR = '솔리드';
        else if (uiStyle === 'glow') styleNameKOR = '네온';
        else if (uiStyle === 'neumorphism') styleNameKOR = '뉴모피즘';
        else if (uiStyle === 'clay') styleNameKOR = '클레이';

        const primary = `hsl(${hue}, ${palette.priS}%, ${palette.priL}%)`;
        const primaryDark = `hsl(${hue}, ${palette.priS}%, ${Math.max(20, palette.priL - 12)}%)`;
        const bg = `linear-gradient(-45deg, hsl(${hue}, ${palette.bgS}%, ${palette.bgL}%), hsl(${(hue + 10) % 360}, ${Math.min(100, palette.bgS + 4)}%, ${Math.max(82, palette.bgL - 4)}%), hsl(${(hue + 24) % 360}, ${Math.max(8, palette.bgS - 8)}%, ${Math.min(98, palette.bgL + 2)}%))`;
        const name = `[${styleNameKOR}/${palette.name}] ${pick(accentAdjectives, rng)} ${pick(accentNouns, rng)}`;

        const vars = {
            '--body-bg': bg,
            '--primary-color': primary,
            '--primary-dark': primaryDark,
            '--text-dark': '#0f172a',
            '--text-gray': '#64748b',
            '--input-text': '#1e293b',
            '--tooltip-bg': 'rgba(15, 23, 42, 0.95)',
            '--tooltip-text': '#ffffff',
            '--effect-color1': `hsl(${hue}, ${palette.effS}%, ${palette.effL}%)`,
            '--effect-color2': `hsl(${(hue + 18) % 360}, ${palette.effS}%, ${Math.min(82, palette.effL + 8)}%)`,
            '--effect-color3': `hsl(${(hue - 18 + 360) % 360}, ${Math.max(10, palette.effS - 12)}%, ${Math.max(48, palette.effL - 9)}%)`
        };

        if (uiStyle === 'glass') {
            vars['--card-bg'] = 'rgba(255, 255, 255, 0.66)';
            vars['--card-border'] = `hsla(${hue}, ${palette.effS}%, ${Math.min(86, palette.effL + 8)}%, 0.92)`;
            vars['--card-backdrop'] = 'blur(24px)';
            vars['--card-shadow'] = `0 16px 40px hsla(${hue}, ${palette.priS}%, ${palette.priL}%, 0.14)`;
            vars['--input-bg'] = 'rgba(255, 255, 255, 0.82)';
            vars['--widget-bg'] = 'rgba(255, 255, 255, 0.72)';
            vars['--widget-hover'] = 'rgba(255, 255, 255, 1)';
            vars['--widget-border'] = `hsla(${hue}, ${palette.effS}%, ${Math.min(86, palette.effL + 8)}%, 0.85)`;
        } else if (uiStyle === 'solid') {
            vars['--card-bg'] = '#ffffff';
            vars['--card-border'] = `hsl(${hue}, ${Math.max(10, palette.effS - 25)}%, ${Math.min(86, palette.effL + 10)}%)`;
            vars['--card-backdrop'] = 'none';
            vars['--card-shadow'] = `0 18px 38px hsla(${hue}, ${palette.priS}%, ${palette.priL}%, 0.12)`;
            vars['--input-bg'] = `hsl(${hue}, ${Math.max(10, palette.bgS - 18)}%, ${Math.min(97, palette.bgL + 1)}%)`;
            vars['--widget-bg'] = `hsl(${hue}, ${Math.max(10, palette.bgS - 16)}%, ${Math.min(96, palette.bgL)}%)`;
            vars['--widget-hover'] = `hsl(${hue}, ${Math.max(12, palette.bgS - 10)}%, ${Math.max(84, palette.bgL - 5)}%)`;
            vars['--widget-border'] = vars['--card-border'];
        } else if (uiStyle === 'glow') {
            vars['--card-bg'] = 'rgba(255, 255, 255, 0.84)';
            vars['--card-border'] = primary;
            vars['--card-backdrop'] = 'blur(10px)';
            vars['--card-shadow'] = `0 0 26px hsla(${hue}, ${palette.priS}%, ${palette.priL}%, 0.42)`;
            vars['--input-bg'] = 'rgba(255,255,255,0.56)';
            vars['--widget-bg'] = 'rgba(255,255,255,0.58)';
            vars['--widget-hover'] = 'rgba(255,255,255,0.92)';
            vars['--widget-border'] = primary;
        } else if (uiStyle === 'neumorphism') {
            const neuBg = `hsl(${hue}, ${Math.max(8, palette.bgS - 34)}%, ${Math.max(86, palette.bgL - 3)}%)`;
            const darkShadow = `hsl(${hue}, ${Math.max(8, palette.bgS - 40)}%, ${Math.max(74, palette.bgL - 12)}%)`;
            vars['--body-bg'] = neuBg;
            vars['--card-bg'] = neuBg;
            vars['--card-border'] = 'transparent';
            vars['--card-backdrop'] = 'none';
            vars['--card-shadow'] = `12px 12px 24px ${darkShadow}, -12px -12px 24px #ffffff`;
            vars['--input-bg'] = 'rgba(255,255,255,0.48)';
            vars['--widget-bg'] = neuBg;
            vars['--widget-hover'] = neuBg;
            vars['--widget-border'] = 'transparent';
        } else if (uiStyle === 'clay') {
            const clayBg = `hsl(${hue}, ${Math.max(8, palette.bgS - 15)}%, ${Math.min(96, palette.bgL + 1)}%)`;
            vars['--card-bg'] = clayBg;
            vars['--card-border'] = 'transparent';
            vars['--card-backdrop'] = 'none';
            vars['--card-shadow'] = '10px 10px 20px rgba(0,0,0,0.1), inset -6px -6px 16px rgba(0,0,0,0.08), inset 6px 6px 16px rgba(255,255,255,0.9)';
            vars['--input-bg'] = 'rgba(255,255,255,0.58)';
            vars['--widget-bg'] = clayBg;
            vars['--widget-hover'] = clayBg;
            vars['--widget-border'] = 'transparent';
        }

        ensureVisibleThemeLines(vars, primary);

        return {
            name,
            type: effectType,
            vars,
            catalog: 'accent',
            catalogIndex: index + 1
        };
    }

    function createAccentThemeCatalog(count) {
        const themes = [];
        for (let i = 0; i < count; i++) {
            themes.push(createAccentTheme(i));
        }
        return themes;
    }

    const deepColorPalettes = [
        { name: '크림슨 레드', hue: 356, bgS: 70, bgL: 94, priS: 78, priL: 44, effS: 88, effL: 62 },
        { name: '루비 레드', hue: 348, bgS: 62, bgL: 93, priS: 72, priL: 38, effS: 84, effL: 58 },
        { name: '스칼렛', hue: 4, bgS: 72, bgL: 94, priS: 82, priL: 47, effS: 88, effL: 63 },
        { name: '청록 틸', hue: 174, bgS: 64, bgL: 92, priS: 74, priL: 34, effS: 78, effL: 58 },
        { name: '딥 청록', hue: 186, bgS: 52, bgL: 90, priS: 64, priL: 31, effS: 70, effL: 55 },
        { name: '고동 브라운', hue: 24, bgS: 38, bgL: 90, priS: 54, priL: 34, effS: 58, effL: 54 },
        { name: '엄버 브라운', hue: 30, bgS: 34, bgL: 88, priS: 48, priL: 31, effS: 50, effL: 52 },
        { name: '브릭 브라운', hue: 16, bgS: 50, bgL: 90, priS: 62, priL: 38, effS: 66, effL: 56 },
        { name: '주홍 버밀리온', hue: 12, bgS: 78, bgL: 93, priS: 86, priL: 48, effS: 90, effL: 64 },
        { name: '카민 주홍', hue: 8, bgS: 74, bgL: 92, priS: 82, priL: 43, effS: 88, effL: 61 }
    ];
    const deepColorAdjectives = ['크림슨', '루비', '스칼렛', '청록', '틸', '딥', '코코아', '엄버', '브릭', '버밀리온', '카민', '벨벳'];
    const deepColorNouns = ['글로우', '라인', '패널', '포커스', '버튼', '코어', '블룸', '스톤', '리버', '프레임', '플레어', '시그널'];

    function createDeepColorTheme(index) {
        const rng = seededRandom(`saedam-theme:deep-color:${index}`);
        const palette = deepColorPalettes[index % deepColorPalettes.length];
        const hueShift = Math.floor(index / deepColorPalettes.length) % 9 - 4;
        const hue = (palette.hue + hueShift + 360) % 360;
        const uiStyle = uiStyles[index % uiStyles.length];
        const effectType = effectsList[(index + 7) % effectsList.length];

        let styleNameKOR = '유리';
        if (uiStyle === 'solid') styleNameKOR = '솔리드';
        else if (uiStyle === 'glow') styleNameKOR = '네온';
        else if (uiStyle === 'neumorphism') styleNameKOR = '뉴모피즘';
        else if (uiStyle === 'clay') styleNameKOR = '클레이';

        const primary = `hsl(${hue}, ${palette.priS}%, ${palette.priL}%)`;
        const primaryDark = `hsl(${hue}, ${palette.priS}%, ${Math.max(18, palette.priL - 12)}%)`;
        const bg = `linear-gradient(-45deg, hsl(${hue}, ${palette.bgS}%, ${palette.bgL}%), hsl(${(hue + 14) % 360}, ${Math.min(100, palette.bgS + 5)}%, ${Math.max(82, palette.bgL - 5)}%), hsl(${(hue - 18 + 360) % 360}, ${Math.max(12, palette.bgS - 10)}%, ${Math.min(97, palette.bgL + 2)}%))`;
        const name = `[${styleNameKOR}/${palette.name}] ${pick(deepColorAdjectives, rng)} ${pick(deepColorNouns, rng)}`;

        const vars = {
            '--body-bg': bg,
            '--primary-color': primary,
            '--primary-dark': primaryDark,
            '--text-dark': '#0f172a',
            '--text-gray': '#64748b',
            '--input-text': '#1e293b',
            '--tooltip-bg': 'rgba(15, 23, 42, 0.95)',
            '--tooltip-text': '#ffffff',
            '--effect-color1': `hsl(${hue}, ${palette.effS}%, ${palette.effL}%)`,
            '--effect-color2': `hsl(${(hue + 22) % 360}, ${Math.min(100, palette.effS + 4)}%, ${Math.min(82, palette.effL + 6)}%)`,
            '--effect-color3': `hsl(${(hue - 24 + 360) % 360}, ${Math.max(18, palette.effS - 14)}%, ${Math.max(44, palette.effL - 8)}%)`
        };

        if (uiStyle === 'glass') {
            vars['--card-bg'] = 'rgba(255, 255, 255, 0.66)';
            vars['--card-border'] = `hsla(${hue}, ${palette.effS}%, ${Math.min(84, palette.effL + 8)}%, 0.9)`;
            vars['--card-backdrop'] = 'blur(24px)';
            vars['--card-shadow'] = `0 16px 40px hsla(${hue}, ${palette.priS}%, ${palette.priL}%, 0.16)`;
            vars['--input-bg'] = 'rgba(255, 255, 255, 0.82)';
            vars['--widget-bg'] = 'rgba(255, 255, 255, 0.72)';
            vars['--widget-hover'] = 'rgba(255, 255, 255, 1)';
            vars['--widget-border'] = vars['--card-border'];
        } else if (uiStyle === 'solid') {
            vars['--card-bg'] = '#ffffff';
            vars['--card-border'] = `hsl(${hue}, ${Math.max(18, palette.effS - 28)}%, ${Math.min(84, palette.effL + 12)}%)`;
            vars['--card-backdrop'] = 'none';
            vars['--card-shadow'] = `0 18px 38px hsla(${hue}, ${palette.priS}%, ${palette.priL}%, 0.13)`;
            vars['--input-bg'] = `hsl(${hue}, ${Math.max(12, palette.bgS - 22)}%, ${Math.min(97, palette.bgL + 2)}%)`;
            vars['--widget-bg'] = `hsl(${hue}, ${Math.max(12, palette.bgS - 20)}%, ${Math.min(96, palette.bgL + 1)}%)`;
            vars['--widget-hover'] = `hsl(${hue}, ${Math.max(14, palette.bgS - 12)}%, ${Math.max(84, palette.bgL - 5)}%)`;
            vars['--widget-border'] = vars['--card-border'];
        } else if (uiStyle === 'glow') {
            vars['--card-bg'] = 'rgba(255, 255, 255, 0.84)';
            vars['--card-border'] = primary;
            vars['--card-backdrop'] = 'blur(10px)';
            vars['--card-shadow'] = `0 0 28px hsla(${hue}, ${palette.priS}%, ${palette.priL}%, 0.44)`;
            vars['--input-bg'] = 'rgba(255,255,255,0.56)';
            vars['--widget-bg'] = 'rgba(255,255,255,0.58)';
            vars['--widget-hover'] = 'rgba(255,255,255,0.92)';
            vars['--widget-border'] = primary;
        } else if (uiStyle === 'neumorphism') {
            const neuBg = `hsl(${hue}, ${Math.max(8, palette.bgS - 34)}%, ${Math.max(85, palette.bgL - 3)}%)`;
            const darkShadow = `hsl(${hue}, ${Math.max(8, palette.bgS - 40)}%, ${Math.max(72, palette.bgL - 13)}%)`;
            vars['--body-bg'] = neuBg;
            vars['--card-bg'] = neuBg;
            vars['--card-border'] = 'transparent';
            vars['--card-backdrop'] = 'none';
            vars['--card-shadow'] = `12px 12px 24px ${darkShadow}, -12px -12px 24px #ffffff`;
            vars['--input-bg'] = 'rgba(255,255,255,0.48)';
            vars['--widget-bg'] = neuBg;
            vars['--widget-hover'] = neuBg;
            vars['--widget-border'] = 'transparent';
        } else if (uiStyle === 'clay') {
            const clayBg = `hsl(${hue}, ${Math.max(10, palette.bgS - 16)}%, ${Math.min(96, palette.bgL + 1)}%)`;
            vars['--card-bg'] = clayBg;
            vars['--card-border'] = 'transparent';
            vars['--card-backdrop'] = 'none';
            vars['--card-shadow'] = '10px 10px 20px rgba(0,0,0,0.1), inset -6px -6px 16px rgba(0,0,0,0.08), inset 6px 6px 16px rgba(255,255,255,0.9)';
            vars['--input-bg'] = 'rgba(255,255,255,0.58)';
            vars['--widget-bg'] = clayBg;
            vars['--widget-hover'] = clayBg;
            vars['--widget-border'] = 'transparent';
        }

        ensureVisibleThemeLines(vars, primary);

        return {
            name,
            type: effectType,
            vars,
            catalog: 'deepColor',
            catalogIndex: index + 1
        };
    }

    function createDeepColorThemeCatalog(count) {
        const themes = [];
        for (let i = 0; i < count; i++) {
            themes.push(createDeepColorTheme(i));
        }
        return themes;
    }

    const seasonalThemes = [
        {
            name: '[계절/새봄] 새봄 그린 브리즈',
            type: 'springBreeze',
            vars: seasonalVars({
                bg: 'linear-gradient(-45deg, #effdf3, #d9f99d, #bbf7d0, #f7fee7)',
                app: '#f7fee7',
                main: '#f0fdf4',
                primary: '#16a34a',
                light: '#dcfce7',
                dark: '#15803d',
                text: '#14351f',
                gray: '#4b6b57',
                border: '#bbf7d0',
                card: 'rgba(255, 255, 255, 0.72)',
                widget: 'rgba(240, 253, 244, 0.82)',
                widgetHover: '#dcfce7',
                shadow: '0 18px 46px rgba(22, 163, 74, 0.16)',
                effect1: '#86efac',
                effect2: '#bef264',
                effect3: '#34d399'
            })
        },
        {
            name: '[계절/벚꽃시즌] 벚꽃 핑크 블룸',
            type: 'cherryBlossoms',
            vars: seasonalVars({
                bg: 'linear-gradient(-45deg, #fff1f8, #ffe4ef, #fbcfe8, #fef3c7)',
                app: '#fff7fb',
                main: '#fff1f8',
                primary: '#db2777',
                light: '#fce7f3',
                dark: '#be185d',
                text: '#4a1d31',
                gray: '#8a4a63',
                border: '#f9a8d4',
                card: 'rgba(255, 245, 250, 0.76)',
                widget: 'rgba(252, 231, 243, 0.78)',
                widgetHover: '#fbcfe8',
                shadow: '0 18px 48px rgba(219, 39, 119, 0.18)',
                effect1: '#f9a8d4',
                effect2: '#fbcfe8',
                effect3: '#ffffff'
            })
        },
        {
            name: '[계절/여름휴가] 여름휴가 오션 스플래시',
            type: 'summerVacation',
            vars: seasonalVars({
                bg: 'linear-gradient(-45deg, #e0faff, #bae6fd, #67e8f9, #fde68a)',
                app: '#ecfeff',
                main: '#f0fdfa',
                primary: '#0891b2',
                light: '#cffafe',
                dark: '#0e7490',
                text: '#12313b',
                gray: '#42606b',
                border: '#67e8f9',
                card: 'rgba(240, 253, 250, 0.72)',
                widget: 'rgba(207, 250, 254, 0.78)',
                widgetHover: '#a5f3fc',
                shadow: '0 18px 46px rgba(8, 145, 178, 0.18)',
                effect1: '#22d3ee',
                effect2: '#fde68a',
                effect3: '#38bdf8'
            })
        },
        {
            name: '[명절/추석] 추석 보름달 한가위',
            type: 'chuseokMoon',
            vars: seasonalVars({
                bg: 'linear-gradient(-45deg, #fff7ed, #ffedd5, #fde68a, #fed7aa)',
                app: '#fff7ed',
                main: '#fffbeb',
                primary: '#b45309',
                light: '#fef3c7',
                dark: '#92400e',
                text: '#422006',
                gray: '#7c5a31',
                border: '#fbbf24',
                card: 'rgba(255, 251, 235, 0.78)',
                widget: 'rgba(254, 243, 199, 0.78)',
                widgetHover: '#fde68a',
                shadow: '0 18px 48px rgba(180, 83, 9, 0.18)',
                effect1: '#f59e0b',
                effect2: '#fde68a',
                effect3: '#92400e'
            })
        },
        {
            name: '[명절/설날] 설날 복주머니 루미너스',
            type: 'seollalRibbons',
            vars: seasonalVars({
                bg: 'linear-gradient(-45deg, #fff7ed, #fee2e2, #dbeafe, #fef3c7)',
                app: '#fffaf0',
                main: '#fff7ed',
                primary: '#dc2626',
                light: '#fee2e2',
                dark: '#991b1b',
                text: '#3f1515',
                gray: '#7a4b4b',
                border: '#fca5a5',
                card: 'rgba(255, 250, 240, 0.8)',
                widget: 'rgba(254, 226, 226, 0.78)',
                widgetHover: '#fecaca',
                shadow: '0 18px 48px rgba(220, 38, 38, 0.16)',
                effect1: '#dc2626',
                effect2: '#f59e0b',
                effect3: '#2563eb'
            })
        },
        {
            name: '[기념일/크리스마스] 크리스마스 스노우 오너먼트',
            type: 'christmasMagic',
            vars: seasonalVars({
                bg: 'linear-gradient(-45deg, #f8fafc, #dcfce7, #fee2e2, #eff6ff)',
                app: '#f8fafc',
                main: '#f0fdf4',
                primary: '#166534',
                light: '#dcfce7',
                dark: '#14532d',
                text: '#102117',
                gray: '#52645a',
                border: '#86efac',
                card: 'rgba(255, 255, 255, 0.76)',
                widget: 'rgba(240, 253, 244, 0.78)',
                widgetHover: '#fee2e2',
                shadow: '0 18px 50px rgba(22, 101, 52, 0.18)',
                effect1: '#ffffff',
                effect2: '#dc2626',
                effect3: '#16a34a'
            })
        },
        {
            name: '[기념일/빼빼로데이] 빼빼로 초코 러브',
            type: 'peperoDay',
            vars: seasonalVars({
                bg: 'linear-gradient(-45deg, #fff7ed, #fed7aa, #fde68a, #fef3c7)',
                app: '#fff7ed',
                main: '#fffbeb',
                primary: '#7c2d12',
                light: '#ffedd5',
                dark: '#431407',
                text: '#35180c',
                gray: '#73533e',
                border: '#fdba74',
                card: 'rgba(255, 247, 237, 0.78)',
                widget: 'rgba(255, 237, 213, 0.78)',
                widgetHover: '#fed7aa',
                shadow: '0 18px 46px rgba(124, 45, 18, 0.18)',
                effect1: '#7c2d12',
                effect2: '#f59e0b',
                effect3: '#fce7f3'
            })
        },
        {
            name: '[기념일/로즈데이] 로즈데이 레드 페탈',
            type: 'roseDay',
            vars: seasonalVars({
                bg: 'linear-gradient(-45deg, #fff1f2, #ffe4e6, #fecdd3, #fce7f3)',
                app: '#fff1f2',
                main: '#fff7f8',
                primary: '#e11d48',
                light: '#ffe4e6',
                dark: '#be123c',
                text: '#4c1020',
                gray: '#854657',
                border: '#fb7185',
                card: 'rgba(255, 241, 242, 0.78)',
                widget: 'rgba(255, 228, 230, 0.78)',
                widgetHover: '#fecdd3',
                shadow: '0 18px 48px rgba(225, 29, 72, 0.18)',
                effect1: '#fb7185',
                effect2: '#e11d48',
                effect3: '#f9a8d4'
            })
        },
        {
            name: '[명절/추석] 한가위 보름달 두둥실',
            type: 'chuseokHangawi',
            vars: seasonalVars({
                bg: 'linear-gradient(-45deg, #0b1026, #1a1b3c, #3a2749, #142041)',
                app: '#0d1225',
                main: '#0d1225',
                primary: '#fcd34d',
                light: 'rgba(252, 211, 77, 0.16)',
                dark: '#f59e0b',
                text: '#fdf6e3',
                gray: '#c4bba6',
                border: 'rgba(252, 211, 77, 0.3)',
                card: 'rgba(23, 28, 58, 0.74)',
                widget: 'rgba(30, 36, 70, 0.7)',
                widgetHover: 'rgba(252, 211, 77, 0.18)',
                shadow: '0 20px 52px rgba(5, 8, 20, 0.55)',
                effect1: '#fcd34d',
                effect2: '#f472b6',
                effect3: '#7c3aed'
            })
        }
    ];

    function seasonalVars(config) {
        return {
            '--body-bg': config.bg,
            '--app-bg': config.app,
            '--main-bg': config.main,
            '--nav-bg': config.card,
            '--primary-color': config.primary,
            '--primary-light': config.light,
            '--primary-dark': config.dark,
            '--text-dark': config.text,
            '--text-gray': config.gray,
            '--border-color': config.border,
            '--border-light': config.light,
            '--input-bg': config.widget,
            '--input-text': config.text,
            '--tooltip-bg': 'rgba(15, 23, 42, 0.95)',
            '--tooltip-text': '#ffffff',
            '--card-bg': config.card,
            '--card-border': config.border,
            '--card-backdrop': 'blur(20px)',
            '--card-shadow': config.shadow,
            '--widget-bg': config.widget,
            '--widget-hover': config.widgetHover,
            '--widget-border': config.border,
            '--effect-color1': config.effect1,
            '--effect-color2': config.effect2,
            '--effect-color3': config.effect3
        };
    }

    function createSeasonalTheme(index) {
        const theme = seasonalThemes[index % seasonalThemes.length];
        return {
            name: theme.name,
            type: theme.type,
            vars: { ...theme.vars },
            catalog: 'seasonal',
            catalogIndex: index + 1
        };
    }

    function createSeasonalThemeCatalog() {
        return seasonalThemes.map((_, index) => createSeasonalTheme(index));
    }

    const systemThemes = [
        {
            name: '[네온/사이트맵] 퀀텀 네비게이션',
            type: 'neonGeometryFlow',
            vars: {
                '--body-bg': 'linear-gradient(135deg, #030616 0%, #07142e 42%, #100a2f 76%, #030616 100%)',
                '--app-bg': '#05091b',
                '--main-bg': '#05091b',
                '--nav-bg': 'rgba(7, 13, 34, 0.9)',
                '--nav-shadow': '0 14px 42px rgba(0, 0, 0, 0.34), 0 1px 0 rgba(103, 232, 249, 0.14)',
                '--primary-color': '#67e8f9',
                '--primary-light': 'rgba(103, 232, 249, 0.14)',
                '--primary-dark': '#22d3ee',
                '--text-dark': '#f8fbff',
                '--text-gray': '#9fb1ca',
                '--border-color': 'rgba(125, 211, 252, 0.24)',
                '--border-light': 'rgba(148, 163, 184, 0.14)',
                '--theme-line-color': 'rgba(103, 232, 249, 0.28)',
                '--dashboard-top-line': 'linear-gradient(90deg, #67e8f9 0%, #60a5fa 46%, #a78bfa 100%)',
                '--card-bg': 'rgba(12, 20, 48, 0.76)',
                '--card-border': 'rgba(103, 232, 249, 0.28)',
                '--card-backdrop': 'blur(24px) saturate(1.25)',
                '--card-shadow': '0 20px 54px rgba(0, 0, 0, 0.34), inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 0 30px rgba(96, 165, 250, 0.08)',
                '--input-bg': 'rgba(7, 14, 36, 0.78)',
                '--input-text': '#f8fbff',
                '--widget-bg': 'rgba(15, 27, 61, 0.72)',
                '--widget-hover': 'rgba(30, 58, 110, 0.62)',
                '--widget-border': 'rgba(125, 211, 252, 0.22)',
                '--tooltip-bg': 'rgba(3, 6, 22, 0.96)',
                '--tooltip-text': '#e6fbff',
                '--effect-color1': '#67e8f9',
                '--effect-color2': '#60a5fa',
                '--effect-color3': '#a78bfa'
            }
        }
    ];

    function createSystemTheme(index) {
        const theme = systemThemes[index % systemThemes.length];
        return {
            name: theme.name,
            type: theme.type,
            vars: { ...theme.vars },
            catalog: 'system',
            catalogIndex: index + 1
        };
    }

    function createSystemThemeCatalog() {
        return systemThemes.map((_, index) => createSystemTheme(index));
    }

    function shuffleThemes(themes, seedText) {
        const rng = seededRandom(seedText);
        for (let i = themes.length - 1; i > 0; i--) {
            const j = Math.floor(rng() * (i + 1));
            const temp = themes[i];
            themes[i] = themes[j];
            themes[j] = temp;
        }
        return themes;
    }

    global.SaedamThemeCatalog = {
        uiStyles,
        paletteNames,
        effectsList,
        adjectives,
        nouns,
        createTheme,
        createThemeCatalog,
        createAccentTheme,
        createAccentThemeCatalog,
        createDeepColorTheme,
        createDeepColorThemeCatalog,
        createSeasonalTheme,
        createSeasonalThemeCatalog,
        createSystemTheme,
        createSystemThemeCatalog,
        getThemeDisplayNumber,
        shuffleThemes
    };
})(window);
