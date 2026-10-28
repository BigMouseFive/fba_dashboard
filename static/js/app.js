/* ============================================================
   FBA 订单数据对比看板 · 前端（Vue 3 全局构建，免打包）
   ============================================================ */
const { createApp, ref, reactive, computed, watch, onMounted, nextTick } = Vue;

/* ---------------------- 工具 ---------------------- */
const nf = new Intl.NumberFormat('zh-CN');

function fmt(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '-';
  return nf.format(Math.round(n));
}

function fmtSigned(n) {
  if (n === null || n === undefined) return '-';
  return (n > 0 ? '+' : '') + fmt(n);
}

function pctText(v, withSign = true) {
  if (v === null || v === undefined) return '—';
  const s = Math.abs(v).toFixed(1) + '%';
  if (!withSign) return s;
  return (v > 0 ? '+' : v < 0 ? '-' : '') + s;
}

function trimTime(ts) {
  if (!ts) return '';
  return String(ts).replace('T', ' ').slice(5, 16);
}

const ICONS = {
  users: '<path d="M16 19v-1.5a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4V19"/><circle cx="9" cy="7" r="3.2"/><path d="M17.5 19v-1.5a4 4 0 0 0-3-3.87"/><path d="M14.5 4.2a3.2 3.2 0 0 1 0 6.1"/>',
  globe: '<circle cx="11" cy="11" r="8"/><path d="M3 11h16"/><path d="M11 3a13 13 0 0 1 0 16a13 13 0 0 1 0-16"/>',
  trend: '<path d="M3 16l5-5 3.5 3.5L21 5"/><path d="M16 5h5v5"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.6"/><rect x="12" y="3" width="7" height="7" rx="1.6"/><rect x="3" y="12" width="7" height="7" rx="1.6"/><rect x="12" y="12" width="7" height="7" rx="1.6"/>',
  upload: '<path d="M12 16V4"/><path d="M7 9l5-5 5 5"/><path d="M4 16v2.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V16"/>',
  download: '<path d="M12 4v12"/><path d="M7 11l5 5 5-5"/><path d="M4 19h16"/>',
  trophy: '<path d="M7 4h10v5a5 5 0 0 1-10 0V4z"/><path d="M7 6H4.5A1.5 1.5 0 0 0 3 7.5C3 9.4 4.6 11 6.5 11H7"/><path d="M17 6h2.5A1.5 1.5 0 0 1 21 7.5C21 9.4 19.4 11 17.5 11H17"/><path d="M12 14v4"/><path d="M8.5 20h7"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  shop: '<path d="M4 9.5V19a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1V9.5"/><path d="M3 9.5 5 4h14l2 5.5"/><path d="M3 9.5a2.6 2.6 0 0 0 4.5 1.8A2.6 2.6 0 0 0 12 11.3a2.6 2.6 0 0 0 4.5 0A2.6 2.6 0 0 0 21 9.5"/>',
  bolt: '<path d="M13 3 5 13h5l-1 8 8-10h-5l1-8z"/>',
};

const Icon = {
  props: { name: String },
  template: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
     stroke-linecap="round" stroke-linejoin="round" v-html="path"></svg>`,
  computed: { path() { return ICONS[this.name] || ''; } },
};

/* ---------------------- 图表封装 ---------------------- */
const Chart = {
  props: { option: Object, height: { type: Number, default: 320 } },
  template: `<div ref="el" class="chart" :style="{ height: height + 'px' }"></div>`,
  setup(props) {
    const el = ref(null);
    let inst = null;
    const draw = () => {
      if (!el.value) return;
      if (!inst) inst = echarts.init(el.value, null, { renderer: 'canvas' });
      inst.setOption(props.option || {}, true);
    };
    watch(() => props.option, () => nextTick(draw), { deep: true });
    onMounted(() => {
      nextTick(draw);
      window.addEventListener('resize', () => inst && inst.resize());
    });
    Vue.onBeforeUnmount(() => { inst && inst.dispose(); inst = null; });
    return { el };
  },
};

/* ---------------------- 主体 ---------------------- */
const App = {
  components: { Icon, Chart },
  template: document.getElementById('tpl').innerHTML,
  setup() {
    /* ---------- 状态 ---------- */
    const meta = ref({ dates: [], months: [], teams: [], batches: [], operator_count: 0 });
    const mode = ref('day');
    const day = ref('');
    const month = ref('');
    const start = ref('');
    const end = ref('');
    const scope = ref('all');
    const teamScope = ref('all');
    const teamMetric = ref('total');   // total=总量  percap=人均
    const loading = ref(false);
    const summary = ref(null);
    const board = ref(null);
    const toasts = ref([]);
    const uploading = ref(false);
    const dragging = ref(false);
    const showImport = ref(false);
    const keyword = ref('');
    const detail = ref(null);
    const loadError = ref('');

    /* ---------- 初始化 ---------- */
    async function loadMeta() {
      const res = await fetch('/api/meta');
      if (!res.ok) throw new Error(`/api/meta ${res.status}`);
      const r = await res.json();
      meta.value = r;
      if (!day.value) day.value = r.latest || r.today;
      if (!month.value) month.value = (r.latest || r.today).slice(0, 7);
      if (!start.value) start.value = r.first || day.value;
      if (!end.value) end.value = r.latest || day.value;
    }

    function query() {
      const p = new URLSearchParams({ mode: mode.value });
      if (mode.value === 'day' || mode.value === 'week') p.set('day', day.value);
      if (mode.value === 'month') p.set('month', month.value);
      if (mode.value === 'custom') { p.set('start', start.value); p.set('end', end.value); }
      return p;
    }

    async function load() {
      loading.value = true;
      try {
        const [rs, rb] = await Promise.all([
          fetch(`/api/summary?${query()}&team_scope=${teamScope.value}`),
          fetch(`/api/operators?${query()}&scope=${scope.value}`),
        ]);
        if (!rs.ok || !rb.ok) {
          throw new Error(`接口返回 ${rs.status}/${rb.status}`);
        }
        const [s, b] = await Promise.all([rs.json(), rb.json()]);
        summary.value = s;
        board.value = b;
        loadError.value = '';
      } catch (e) {
        loadError.value = e.message || '未知错误';
        toast('数据加载失败：' + loadError.value, 'err');
      } finally {
        loading.value = false;
      }
    }

    onMounted(async () => {
      try {
        await loadMeta();
      } catch (e) {
        loadError.value = '读取基础信息失败：' + e.message;
        toast(loadError.value, 'err');
      }
      await load();
      window.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        if (detail.value) { detail.value = null; return; }
        if (showImport.value) showImport.value = false;
      });
    });

    // 弹窗打开时锁定页面滚动
    watch(showImport, (open) => {
      document.body.classList.toggle('modal-open', open);
    });

    watch([mode, day, month, start, end], () => {
      if (mode.value === 'custom' && (!start.value || !end.value)) return;
      load();
    });
    // 运营口径 / 团队口径变化都要重取（summary 的团队数据随 teamScope 变）
    watch([scope, teamScope], () => load());

    /* ---------- 提示 ---------- */
    function toast(msg, kind = 'ok') {
      const id = Math.random();
      toasts.value.push({ id, msg, kind });
      setTimeout(() => { toasts.value = toasts.value.filter((t) => t.id !== id); }, 4200);
    }

    /* ---------- 上传 ---------- */
    async function pick(kind) {
      const input = document.createElement('input');
      input.type = 'file';
      input.accept = '.xlsx,.xlsm';
      input.multiple = kind === 'orders';
      input.onchange = () => handleFiles([...input.files], kind);
      input.click();
    }

    async function handleFiles(files, kind) {
      if (!files.length) return;
      uploading.value = true;
      for (const f of files) {
        const fd = new FormData();
        fd.append('file', f);
        try {
          const res = await fetch(`/api/upload/${kind}`, { method: 'POST', body: fd });
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || '上传失败');
          if (kind === 'orders') {
            toast(`已导入 ${data.biz_date}：${fmt(data.total_qty)} 件 / ${fmt(data.row_count)} 行`, 'ok');
          } else {
            toast(`运营代号已更新：${data.operators} 条`, 'ok');
          }
        } catch (e) {
          toast(`${f.name} 导入失败：${e.message}`, 'err');
        }
      }
      uploading.value = false;
      await loadMeta();
      await load();
    }

    function onDrop(e) {
      dragging.value = false;
      handleFiles([...e.dataTransfer.files], 'orders');
    }

    /* ---------- 明细 ---------- */
    async function openDetail(it) {
      detail.value = { ...it, loading: true, items: [] };
      try {
        const r = await fetch(`/api/operators/${encodeURIComponent(it.code)}/daily`).then((x) => x.json());
        detail.value = { ...it, loading: false, items: r.items || [] };
      } catch {
        detail.value = { ...it, loading: false, items: [] };
      }
    }
    function closeDetail() { detail.value = null; }

    async function retry() {
      loadError.value = '';
      loading.value = true;
      try {
        await loadMeta();
      } catch (e) {
        loadError.value = '读取基础信息失败：' + e.message;
      }
      await load();
    }

    /* ---------- 派生数据 ---------- */
    const rangeLabel = computed(() => summary.value?.range?.label || '');
    const teams = computed(() => summary.value?.by_team || []);
    const countries = computed(() => summary.value?.by_country || []);
    const ops = computed(() => board.value?.items || []);
    const top10 = computed(() => ops.value.slice(0, 10));

    // 表格分页：默认只露出前 N 名，避免首页被 74 行淹没
    const pageSize = ref(15);
    const showAllOps = ref(false);
    const allFiltered = computed(() => {
      const k = keyword.value.trim().toUpperCase();
      if (!k) return ops.value;
      return ops.value.filter((o) =>
        o.code.toUpperCase().includes(k) || (o.name || '').includes(keyword.value.trim()) ||
        (o.team || '').includes(keyword.value.trim()));
    });
    const filteredOps = computed(() =>
      showAllOps.value ? allFiltered.value : allFiltered.value.slice(0, pageSize.value));
    const hiddenCount = computed(() => Math.max(0, allFiltered.value.length - filteredOps.value.length));
    watch([keyword, scope], () => { showAllOps.value = false; });

    /* ---------- 24 小时时段热力 ---------- */
    const hourChart = computed(() => {
      const rows = summary.value?.by_hour || [];
      const hours = rows.map((r) => r.hour);
      // 热力 data: [x索引, y索引, 值]  y: 0=阿联酋 1=沙特
      const data = [];
      let max = 1;
      rows.forEach((r, i) => {
        data.push([i, 0, r.UAE]);
        data.push([i, 1, r.KSA]);
        max = Math.max(max, r.UAE, r.KSA);
      });
      return {
        grid: { left: 10, right: 66, top: 16, bottom: 40, containLabel: true },
        tooltip: {
          position: 'top',
          backgroundColor: 'rgba(27,31,42,.94)', borderWidth: 0,
          textStyle: { color: '#fff', fontSize: 12 },
          formatter: (p) => {
            const r = rows[p.value[0]];
            const name = p.value[1] === 0 ? '阿联酋' : '沙特';
            return `${r.hour}:00 ${name}<br/>${fmt(p.value[2])} 件`;
          },
        },
        xAxis: {
          type: 'category', data: hours,
          splitArea: { show: false },
          axisLine: { lineStyle: { color: '#e6e8ef' } },
          axisTick: { show: false },
          axisLabel: { color: '#8d94a6', fontSize: 10.5, interval: 1 },
        },
        yAxis: {
          type: 'category', data: ['阿联酋', '沙特'],
          axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: '#5b6274', fontSize: 12 },
        },
        visualMap: {
          min: 0, max,
          calculable: false,
          orient: 'vertical',
          right: 4, top: 'center',
          itemWidth: 10, itemHeight: 84,
          text: ['高', '低'],
          textStyle: { color: '#8d94a6', fontSize: 10.5 },
          inRange: { color: ['#eef1fe', '#b9bffa', '#7b85f0', '#4e5be4', '#3b46c9'] },
        },
        series: [{
          type: 'heatmap',
          data,
          label: {
            show: true,
            fontSize: 10,
            fontWeight: 600,
            color: '#fff',
            // 每行只标最高的那一格，避免相邻格子数字连成一条
            formatter: (p) => {
              const row = p.value[1];
              const rowMax = Math.max(...data.filter((d) => d[1] === row).map((d) => d[2]));
              return p.value[2] === rowMax ? fmt(p.value[2]) : '';
            },
          },
          itemStyle: { borderColor: '#fff', borderWidth: 2, borderRadius: 3 },
          emphasis: { itemStyle: { borderColor: '#1b1f2a', borderWidth: 1.5 } },
        }],
      };
    });


    // 74 个运营在两个站点都有出单，原来的「双站点」筛选与「全部站点」完全等价，已移除
    const SCOPES = [
      { key: 'all', label: '全部站点' },
      { key: 'uae', label: '阿联酋' },
      { key: 'ksa', label: '沙特' },
    ];
    const scopes = SCOPES;
    const teamScopes = SCOPES;

    const teamScopeLabel = computed(
      () => (teamScopes.find((s) => s.key === teamScope.value) || teamScopes[0]).label,
    );

    // 按"总量"或"人均"重排，并重算名次（PK 榜与团队环共用）
    const teamRank = computed(() => {
      const list = (summary.value?.by_team || []).map((t) => ({
        ...t,
        percap: t.operators ? +(t.qty / t.operators).toFixed(1) : 0,
        metric: teamMetric.value === 'percap'
          ? (t.operators ? +(t.qty / t.operators).toFixed(1) : 0)
          : t.qty,
      }));
      list.sort((a, b) => b.metric - a.metric);
      const prevRank = [...list]
        .sort((a, b) => b.prev_qty - a.prev_qty)
        .reduce((m, t, i) => (m[t.team] = i + 1, m), {});
      list.forEach((t, i) => {
        t.rank = i + 1;
        t.rank_prev = prevRank[t.team];
        t.rank_delta = t.rank_prev - t.rank;
      });
      return list;
    });

    const modes = [
      { key: 'day', label: '日' },
      { key: 'week', label: '周' },
      { key: 'month', label: '月' },
      { key: 'custom', label: '自定义' },
    ];

    /* ---------- 图表配置 ---------- */
    // 团队占比环（替代原来的团队柱状图，避免与下方 PK 榜重复）
    // 主色系阶梯（深浅区分相邻团队），末位用暖色突出尾部团队
    // 8 个团队用色相分散的配色，相邻扇区色差明显，不再用同色系深浅
    const TEAM_PALETTE = ['#3b46c9', '#e8734a', '#12b886', '#f0a33c',
                          '#8b5cf6', '#2f9ed8', '#d94a86', '#7f8a9e'];

    const teamChart = computed(() => {
      const list = teamRank.value;
      const total = summary.value?.team_total || list.reduce((a, b) => a + b.qty, 0) || 1;
      const data = list.map((t, i) => ({
        name: t.team,
        value: t.qty,
        itemStyle: { color: TEAM_PALETTE[i % TEAM_PALETTE.length] },
      }));

      return {
        tooltip: {
          trigger: 'item',
          backgroundColor: 'rgba(27,31,42,.94)', borderWidth: 0,
          textStyle: { color: '#fff', fontSize: 12 },
          formatter: (p) => {
            const it = list[p.dataIndex];
            return `<b>${it.team}</b><br/>${fmt(it.qty)} 件 · 占比 ${it.share}%<br/>`
              + `人均 ${fmt(it.percap)} 件 · ${it.operators} 人<br/>`
              + `环比 ${pctText(it.delta_pct)}`;
          },
        },
        // 图例承担"团队名 + 件数 + 占比"，环内只留中心总数，避免标签互相压住
        legend: {
          bottom: 2,
          left: 'center',
          width: '92%',
          itemWidth: 8,
          itemHeight: 8,
          itemGap: 12,
          textStyle: { fontSize: 11, color: '#5b6274', lineHeight: 15 },
          formatter: (name) => {
            const it = list.find((x) => x.team === name);
            return it ? `${name} ${it.share}%` : name;
          },
          data: list.map((t) => t.team),
        },
        series: [{
          type: 'pie',
          radius: ['50%', '70%'],
          center: ['50%', '42%'],
          minAngle: 4,
          avoidLabelOverlap: true,
          percentPrecision: 1,
          itemStyle: { borderColor: '#fff', borderWidth: 2 },
          // 环上不放百分比，信息交给下方图例与悬停提示，避免小扇区文字挤压
          label: { show: false },
          labelLine: { show: false },
          emphasis: { scale: true, scaleSize: 5 },
          data,
        }],
        graphic: [{
          type: 'group',
          left: 'center', top: '42%', bounding: 'raw',
          children: [
            {
              type: 'text', top: -14,
              style: {
                text: fmt(total),
                fontSize: 21, fontWeight: 700, fill: '#1b1f2a',
                textAlign: 'center', textVerticalAlign: 'middle',
              },
            },
            {
              type: 'text', top: 10,
              style: {
                text: '团队合计 / 件',
                fontSize: 11, fill: '#8d94a6',
                textAlign: 'center', textVerticalAlign: 'middle',
              },
            },
          ],
        }],
      };
    });

    const dailyChart = computed(() => {
      const trend = summary.value?.trend || [];
      const r = summary.value?.range;
      const inRange = (d) => r && d >= r.start && d <= r.end;
      const list = trend.length ? trend : (summary.value?.daily || []);
      return {
        grid: { left: 8, right: 22, top: 24, bottom: 6, containLabel: true },
        tooltip: {
          trigger: 'axis', backgroundColor: 'rgba(27,31,42,.94)', borderWidth: 0,
          textStyle: { color: '#fff', fontSize: 12 },
          formatter: (p) => {
            const d = list[p[0].dataIndex];
            return `<b>${d.biz_date}</b><br/>${fmt(d.qty)} 件 · ${fmt(d.orders)} 订单`;
          },
        },
        xAxis: {
          type: 'category', data: list.map((d) => d.biz_date.slice(5)),
          boundaryGap: false,
          axisLine: { lineStyle: { color: '#e6e8ef' } }, axisTick: { show: false },
          axisLabel: { color: '#8d94a6', fontSize: 11 },
        },
        yAxis: {
          type: 'value', splitLine: { lineStyle: { color: '#f0f1f6' } },
          axisLabel: { color: '#8d94a6', fontSize: 11 },
        },
        series: [{
          type: 'line', data: list.map((d) => d.qty), smooth: 0.25,
          symbol: 'circle', symbolSize: (v, p) => (inRange(list[p.dataIndex].biz_date) ? 9 : 5),
          lineStyle: { width: 2.5, color: '#4e5be4' },
          itemStyle: {
            color: (p) => (inRange(list[p.dataIndex].biz_date) ? '#4e5be4' : '#c3c8dd'),
            borderColor: '#fff', borderWidth: 2,
          },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(78,91,228,.18)' }, { offset: 1, color: 'rgba(78,91,228,0)' },
            ]),
          },
          markArea: r && list.length ? {
            silent: true,
            itemStyle: { color: 'rgba(78,91,228,.06)' },
            data: [[{ xAxis: r.start.slice(5) }, { xAxis: r.end.slice(5) }]],
          } : undefined,
        }],
      };
    });

    const topChart = computed(() => {
      const list = [...top10.value].reverse();
      return {
        grid: { left: 8, right: 62, top: 10, bottom: 8, containLabel: true },
        tooltip: {
          trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: 'rgba(27,31,42,.94)', borderWidth: 0,
          textStyle: { color: '#fff', fontSize: 12 },
          formatter: (p) => {
            const it = list[p[0].dataIndex];
            return `<b>${it.code} ${it.name || ''}</b><br/>${it.team}<br/>合计 ${fmt(it.qty)} 件<br/>UAE ${fmt(it.uae)} / KSA ${fmt(it.ksa)}`;
          },
        },
        xAxis: { type: 'value', splitLine: { lineStyle: { color: '#f0f1f6' } }, axisLabel: { color: '#8d94a6', fontSize: 11 } },
        yAxis: { type: 'category', data: list.map((t) => t.code), axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: '#5b6274', fontSize: 12, fontWeight: 600 } },
        series: [{
          type: 'bar', barWidth: 16,
          data: list.map((t) => ({ value: t.qty, itemStyle: { borderRadius: [0, 6, 6, 0] } })),
          itemStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
              { offset: 0, color: '#f7b955' }, { offset: 1, color: '#ef7f2c' },
            ]),
          },
          label: { show: true, position: 'right', color: '#5b6274', fontSize: 11.5, formatter: (p) => fmt(p.value) },
        }],
      };
    });

    /* ---------- 导出 ---------- */
    function exportCsv(kind) {
      const p = query();
      p.set('kind', kind);
      window.location.href = `/api/export.csv?${p}`;
    }

    /* ---------- 视图辅助 ---------- */
    function rankClass(i) { return ['r1', 'r2', 'r3'][i] || ''; }
    const podium = computed(() => top10.value.slice(0, 3));

    return {
      meta, mode, day, month, start, end, scope, loading, summary, board,
      toasts, uploading, dragging, showImport, keyword, detail, loadError,
      modes, scopes, teamScopes, teamScope, teamScopeLabel, rangeLabel, teams, countries, ops, top10, filteredOps, podium,
      allFiltered, showAllOps, hiddenCount, pageSize,
      teamChart, dailyChart, topChart, hourChart,
      teamMetric, teamRank,
      fmt, fmtSigned, pctText, trimTime, rankClass, exportCsv, openDetail, closeDetail,
      pick, onDrop, handleFiles, retry,
    };
  },
};

createApp(App).mount('#app');
