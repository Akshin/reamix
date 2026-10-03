-- reamix: функции над проектом REAPER, доступные запросам как таблица rx.
-- Нумерация: треки и плагины с 1 (как в интерфейсе), 0 или 'master' — мастер;
-- параметры плагинов с 0 (как в API REAPER).

local json, root = ...
local r = reaper
local A = json.array

local rx = {}

-- Ожидаемая ошибка (не найден трек, плагин не добавился): мост покажет её без трассировки.
local Failure = { __tostring = function(e) return e.reamix end }
local function fail(msg) error(setmetatable({ reamix = msg }, Failure), 0) end

local function db_to_gain(db) return 10 ^ (db / 20) end

local function contains(haystack, needle)
  return haystack:lower():find(needle:lower(), 1, true) ~= nil
end

-- Один кандидат из списка по ссылке-строке: сначала точное имя, потом вхождение.
local function pick(items, ref, what)
  local exact, partial = {}, {}
  for _, it in ipairs(items) do
    if it.name == ref then exact[#exact + 1] = it
    elseif contains(it.name, ref) then partial[#partial + 1] = it end
  end
  local found = #exact > 0 and exact or partial
  if #found == 1 then return found[1] end
  if #found == 0 then
    -- Короткий список проще показать, чем заставлять спрашивать его отдельно.
    local have = {}
    for i = 1, math.min(#items, 12) do have[i] = items[i].name end
    local hint = (#items > 0 and #items <= 12) and (' (есть: ' .. table.concat(have, ', ') .. ')') or ''
    fail(what .. ' не найден: ' .. ref .. hint)
  end
  -- У плагина бывают сотни похожих параметров: весь список в ошибке бесполезен.
  local names, limit = {}, 8
  for i = 1, math.min(#found, limit) do names[i] = found[i].label end
  local rest = #found > limit and (' и ещё ' .. (#found - limit)) or ''
  fail(what .. ' «' .. ref .. '» неоднозначен: ' .. table.concat(names, ', ') .. rest)
end

-- ── Проект ────────────────────────────────────────────────────────────────

local function project_srate()
  local srate = r.GetSetProjectInfo(0, 'PROJECT_SRATE', 0, false)
  if r.GetSetProjectInfo(0, 'PROJECT_SRATE_USE', 0, false) == 0 then
    local ok, device = r.GetAudioDeviceInfo('SRATE')
    if ok then srate = tonumber(device) or srate end
  end
  return srate
end

function rx.project()
  local _, path = r.EnumProjects(-1)
  local bpm, beats = r.GetProjectTimeSignature2(0)
  return {
    path = path or '',
    bpm = bpm,
    beats = beats,
    srate = project_srate(),
    length = r.GetProjectLength(0),
    dirty = r.IsProjectDirty(0) == 1,
    rev = r.GetProjectStateChangeCount(0),
    tempo_markers = r.CountTempoTimeSigMarkers(0),
  }
end

function rx.markers()
  local out = A {}
  local _, n_markers, n_regions = r.CountProjectMarkers(0)
  for i = 0, n_markers + n_regions - 1 do
    local _, is_region, pos, rgn_end, name, number = r.EnumProjectMarkers3(0, i)
    out[#out + 1] = {
      n = number, region = is_region, pos = pos,
      ['end'] = is_region and rgn_end or nil, name = name,
    }
  end
  return out
end

-- Линии сетки проекта на участке t0..t1. step — шаг в четвертях; сетка идёт от начала
-- каждого такта, как линейка REAPER. Про каждую линию: время, номер шага от начала такта
-- и длина доли в четвертях (4 / знаменатель размера).
function rx.grid(t0, t1, step)
  local t, k, beat = A {}, A {}, A {}
  local _, measure = r.TimeMap2_timeToBeats(0, t0)
  while true do
    local start, qn0, qn1, _, denom = r.TimeMap_GetMeasureInfo(0, measure)
    if start >= t1 or qn1 <= qn0 then break end
    local i = 0
    while qn0 + i * step < qn1 - 1e-9 do
      local at = r.TimeMap2_QNToTime(0, qn0 + i * step)
      if at >= t0 - 1e-9 and at < t1 then
        t[#t + 1], k[#k + 1], beat[#beat + 1] = at, i, 4 / denom
      end
      i = i + 1
    end
    measure = measure + 1
  end
  return { t = t, k = k, beat = beat }
end

-- ── Треки ─────────────────────────────────────────────────────────────────

local function track_name(tr)
  local _, name = r.GetSetMediaTrackInfo_String(tr, 'P_NAME', '', false)
  return name
end

local function track_number(tr)
  local n = r.GetMediaTrackInfo_Value(tr, 'IP_TRACKNUMBER')
  return n == -1 and 0 or math.floor(n)
end

function rx.track(ref)
  if type(ref) ~= 'number' and type(ref) ~= 'string' then return ref end
  if ref == 0 or ref == 'master' then return r.GetMasterTrack(0) end
  if type(ref) == 'number' then
    return r.GetTrack(0, ref - 1) or fail('нет трека №' .. ref)
  end
  local items = {}
  for i = 0, r.CountTracks(0) - 1 do
    local tr = r.GetTrack(0, i)
    if r.GetTrackGUID(tr) == ref then return tr end
    local name = track_name(tr)
    items[#items + 1] = { name = name, label = (i + 1) .. ' ' .. name, tr = tr }
  end
  return pick(items, ref, 'трек').tr
end

local function fx_list(tr)
  local out = A {}
  for i = 0, r.TrackFX_GetCount(tr) - 1 do
    local _, name = r.TrackFX_GetFXName(tr, i, '')
    local _, preset = r.TrackFX_GetPreset(tr, i, '')
    out[#out + 1] = {
      i = i + 1,
      name = name,
      guid = r.TrackFX_GetFXGUID(tr, i),
      enabled = r.TrackFX_GetEnabled(tr, i),
      offline = r.TrackFX_GetOffline(tr, i),
      params = r.TrackFX_GetNumParams(tr, i),
      preset = preset ~= '' and preset or nil,
    }
  end
  return out
end

local function send_list(tr)
  local out = A {}
  for i = 0, r.GetTrackNumSends(tr, 0) - 1 do
    local function get(key) return r.GetTrackSendInfo_Value(tr, 0, i, key) end
    local send = {
      i = i + 1, vol = get('D_VOL'), pan = get('D_PAN'),
      mute = get('B_MUTE') == 1, mode = math.floor(get('I_SENDMODE')),
      dst_chan = math.floor(get('I_DSTCHAN')),
    }
    -- Указатель на трек-получатель доступен не во всех сборках; запасной путь — имя.
    local ok, dest = pcall(function()
      local ptr = r.GetTrackSendInfo_Value(tr, 0, i, 'P_DESTTRACK')
      if r.ValidatePtr2(0, ptr, 'MediaTrack*') then return { track_number(ptr), r.GetTrackGUID(ptr) } end
    end)
    if ok and dest then send.dest, send.dest_guid = dest[1], dest[2] end
    local _, name = r.GetTrackSendName(tr, i, '')
    send.dest_name = name
    out[#out + 1] = send
  end
  return out
end

local function envelope_list(tr)
  local out = A {}
  for i = 0, r.CountTrackEnvelopes(tr) - 1 do
    local env = r.GetTrackEnvelope(tr, i)
    local _, name = r.GetEnvelopeName(env, '')
    local _, active = r.GetSetEnvelopeInfo_String(env, 'ACTIVE', '', false)
    out[#out + 1] = {
      name = name,
      points = r.CountEnvelopePoints(env),
      auto_items = r.CountAutomationItems(env),
      active = active == '1',
    }
  end
  return out
end

-- Сдвиг воспроизведения дорожки в мс: плюс — звучит позже. Это «Media playback offset»
-- в окне роутинга трека; айтемы остаются на месте.
local function play_offset(tr)
  local flag = math.floor(r.GetMediaTrackInfo_Value(tr, 'I_PLAY_OFFSET_FLAG'))
  if flag & 1 == 1 then return 0 end
  local value = r.GetMediaTrackInfo_Value(tr, 'D_PLAY_OFFSET')
  return flag & 2 == 2 and value / project_srate() * 1000 or value * 1000
end

local function track_info(tr)
  local function get(key) return r.GetMediaTrackInfo_Value(tr, key) end
  local n_items = r.CountTrackMediaItems(tr)
  local first, last
  for i = 0, n_items - 1 do
    local item = r.GetTrackMediaItem(tr, i)
    local pos = r.GetMediaItemInfo_Value(item, 'D_POSITION')
    local stop = pos + r.GetMediaItemInfo_Value(item, 'D_LENGTH')
    if not first or pos < first then first = pos end
    if not last or stop > last then last = stop end
  end
  return {
    n = track_number(tr),
    guid = r.GetTrackGUID(tr),
    name = track_name(tr),
    depth = r.GetTrackDepth(tr),
    folder = get('I_FOLDERDEPTH') == 1,
    vol = get('D_VOL'),
    pan = get('D_PAN'),
    width = get('D_WIDTH'),
    mute = get('B_MUTE') == 1,
    solo = get('I_SOLO') ~= 0,
    phase = get('B_PHASE') == 1,
    offset_ms = play_offset(tr),
    main_send = get('B_MAINSEND') == 1,
    channels = math.floor(get('I_NCHAN')),
    automode = math.floor(get('I_AUTOMODE')),
    fx = fx_list(tr),
    input_fx = r.TrackFX_GetRecCount(tr),
    sends = send_list(tr),
    receives = r.GetTrackNumSends(tr, -1),
    hw_outs = r.GetTrackNumSends(tr, 1),
    envelopes = envelope_list(tr),
    items = n_items,
    items_start = first,
    items_end = last,
  }
end

function rx.track_info(ref) return track_info(rx.track(ref)) end

-- Скелет проекта: всё, что нужно, чтобы понять устройство микса.
function rx.skeleton()
  local tracks = A {}
  for i = 0, r.CountTracks(0) - 1 do tracks[#tracks + 1] = track_info(r.GetTrack(0, i)) end
  return {
    project = rx.project(),
    master = track_info(r.GetMasterTrack(0)),
    tracks = tracks,
    markers = rx.markers(),
  }
end

function rx.track_set(ref, props)
  local tr = rx.track(ref)
  local function set(key, v) r.SetMediaTrackInfo_Value(tr, key, v) end
  for k, v in pairs(props) do
    if k == 'vol_db' then set('D_VOL', db_to_gain(v))
    elseif k == 'vol' then set('D_VOL', v)
    elseif k == 'pan' then set('D_PAN', v)
    elseif k == 'width' then set('D_WIDTH', v)
    elseif k == 'mute' then set('B_MUTE', v and 1 or 0)
    elseif k == 'solo' then set('I_SOLO', v and 1 or 0)
    elseif k == 'phase' then set('B_PHASE', v and 1 or 0)
    elseif k == 'offset_ms' then
      if v ~= 0 and r.CountTrackMediaItems(tr) == 0 then
        fail('сдвиг действует на айтемы самой дорожки, а у «' .. track_name(tr) .. '» их нет: у папки ставь его на её дорожки')
      end
      -- Ноль — сдвиг выключен, как у нового трека.
      set('I_PLAY_OFFSET_FLAG', v == 0 and 1 or 0)
      set('D_PLAY_OFFSET', v / 1000)
    elseif k == 'name' then r.GetSetMediaTrackInfo_String(tr, 'P_NAME', v, true)
    else fail('неизвестное свойство трека: ' .. tostring(k)) end
  end
  return track_info(tr)
end

-- ── Плагины ───────────────────────────────────────────────────────────────

-- Индекс плагина в API REAPER (с 0) по номеру (с 1) или части имени.
function rx.fx(track, ref)
  local tr = rx.track(track)
  if type(ref) == 'number' then
    if ref < 1 or ref > r.TrackFX_GetCount(tr) then fail('нет плагина №' .. ref) end
    return tr, ref - 1
  end
  local items = {}
  for _, fx in ipairs(fx_list(tr)) do
    items[#items + 1] = { name = fx.name, label = fx.i .. ' ' .. fx.name, i = fx.i - 1 }
  end
  return tr, pick(items, ref, 'плагин').i
end

local function param_name(tr, fx, p)
  local _, name = r.TrackFX_GetParamName(tr, fx, p, '')
  return name
end

local function param_fmt(tr, fx, p)
  local _, text = r.TrackFX_GetFormattedParamValue(tr, fx, p, '')
  return text
end

local function param_info(tr, fx, p)
  local raw, min, max = r.TrackFX_GetParam(tr, fx, p)
  return {
    p = p, name = param_name(tr, fx, p), fmt = param_fmt(tr, fx, p),
    norm = r.TrackFX_GetParamNormalized(tr, fx, p), raw = raw, min = min, max = max,
  }
end

function rx.param(tr, fx, ref)
  local count = r.TrackFX_GetNumParams(tr, fx)
  if type(ref) == 'number' then
    if ref < 0 or ref >= count then fail('нет параметра №' .. ref) end
    return ref
  end
  local items = {}
  for p = 0, count - 1 do
    local name = param_name(tr, fx, p)
    items[#items + 1] = { name = name, label = p .. ' ' .. name, p = p }
  end
  return pick(items, ref, 'параметр').p
end

-- Параметры плагина; match — оставить только те, в имени которых есть подстрока.
function rx.fx_params(track, fxref, match)
  local tr, fx = rx.fx(track, fxref)
  local _, name = r.TrackFX_GetFXName(tr, fx, '')
  local params = A {}
  for p = 0, r.TrackFX_GetNumParams(tr, fx) - 1 do
    if not match or contains(param_name(tr, fx, p), match) then
      params[#params + 1] = param_info(tr, fx, p)
    end
  end
  local _, ident = r.TrackFX_GetNamedConfigParm(tr, fx, 'fx_ident')
  return { fx = fx + 1, name = name, ident = ident, total = r.TrackFX_GetNumParams(tr, fx), params = params }
end

local SCRATCH = 'reamix: служебное действие'

local function is_edit(label)
  return label ~= nil and label:find('^reamix: ') ~= nil and label ~= SCRATCH
end

-- Правка, которую сейчас слушают в состоянии «до» (reamix ab): она лежит в redo.
-- Любая новая запись в истории её уничтожит, поэтому правки в это время запрещены.
function rx.preview()
  local label = r.GetExtState('reamix', 'preview')
  if label == '' then return nil end
  if r.Undo_CanRedo2(0) ~= label then
    r.SetExtState('reamix', 'preview', '', false)
    return nil
  end
  return label
end

function rx.ensure_no_preview()
  local label = rx.preview()
  if label then
    fail('сейчас включено состояние «до» для «' .. label .. '»: сначала reamix ab after (оставить) или reamix undo (откатить)')
  end
end

-- Выполняет fn и стирает её след: и в проекте, и в истории undo. Для служебных действий,
-- которые не должны вставать между правками (иначе reamix undo откатит не то).
function rx.scratch(fn)
  rx.ensure_no_preview()
  local label = SCRATCH
  r.Undo_BeginBlock2(0)
  r.PreventUIRefresh(1)
  local ok, res = pcall(fn)
  r.PreventUIRefresh(-1)
  r.Undo_EndBlock2(0, label, -1)
  if r.Undo_CanUndo2(0) == label then r.Undo_DoUndo2(0) end
  if not ok then error(res, 0) end
  return res
end

-- Значения параметров плагина по умолчанию: API их не отдаёт, поэтому на временном
-- треке создаётся чистый экземпляр того же плагина.
function rx.fx_defaults(track, fxref)
  local tr, fx = rx.fx(track, fxref)
  local _, name = r.TrackFX_GetNamedConfigParm(tr, fx, 'fx_name')
  local _, ident = r.TrackFX_GetNamedConfigParm(tr, fx, 'fx_ident')
  local add = name:gsub('^(%w-)i?: ', '%1:')
  local norms = rx.scratch(function()
    local n = r.CountTracks(0)
    r.InsertTrackAtIndex(n, false)
    local tmp = r.GetTrack(0, n)
    local i = r.TrackFX_AddByName(tmp, add, false, -1)
    local out = A {}
    if i >= 0 then
      for p = 0, r.TrackFX_GetNumParams(tmp, i) - 1 do out[#out + 1] = r.TrackFX_GetParamNormalized(tmp, i, p) end
    end
    r.DeleteTrack(tmp)
    if i < 0 then fail('чистый экземпляр не создался: ' .. add) end
    return out
  end)
  return { ident = ident, name = name, norms = norms }
end

-- Число из текста, которым плагин показывает значение: «1.2 kHz» → 1200,
-- «30 ms» → 0.03, «-inf dB» → -inf. Не число («Off») → nil. Второе значение — единица.
local function parse_number(text)
  if type(text) == 'number' then return text, '' end
  local s = text:lower()
  if s:match('^%s*%-%s*inf') then return -math.huge, '' end
  if s:match('^%s*%+?%s*inf') then return math.huge, '' end
  local num, unit = s:match('^%s*([%-+]?%d*%.?%d+)%s*([%a%%]*)')
  local n = tonumber(num)
  if not n then return nil end
  if unit == 'ms' then return n / 1000, unit end
  if unit:sub(1, 1) == 'k' then return n * 1000, unit end
  return n, unit
end

local SECONDS = { s = true, sec = true, secs = true, seconds = true }

-- Как плагин покажет значение x (0..1). Спрашиваем, не трогая параметр; если плагин
-- так не умеет — ставим и читаем (тогда вызывающий возвращает параметр на место).
local function text_at(tr, fx, p, x)
  local ok, text = r.TrackFX_FormatParamValueNormalized(tr, fx, p, x, '')
  if ok and text ~= '' then return text end
  r.TrackFX_SetParamNormalized(tr, fx, p, x)
  return param_fmt(tr, fx, p)
end

-- Ставит значение и убеждается, что плагин его принял. CLAP-плагины применяют
-- изменения в аудиопотоке: с задержкой, а при закрытом аудиоустройстве — никогда.
local function set_norm(tr, fx, p, x)
  local before = r.TrackFX_GetParamNormalized(tr, fx, p)
  r.TrackFX_SetParamNormalized(tr, fx, p, x)
  local function applied()
    if r.TrackFX_GetParamNormalized(tr, fx, p) ~= before then return true end
    -- Значение на месте: это нормально, только если менять было нечего.
    local ok_a, a = r.TrackFX_FormatParamValueNormalized(tr, fx, p, x, '')
    local ok_b, b = r.TrackFX_FormatParamValueNormalized(tr, fx, p, before, '')
    return not ok_a or not ok_b or a == b
  end
  if applied() then return end
  local opened = r.Audio_IsRunning() == 0
  if opened then r.Audio_Init() end
  local deadline = r.time_precise() + 0.5
  repeat r.TrackFX_SetParamNormalized(tr, fx, p, x) until applied() or r.time_precise() > deadline
  if opened then r.Audio_Quit() end
  if not applied() then fail('плагин не принял значение параметра «' .. param_name(tr, fx, p) .. '»') end
end

-- Подбирает нормализованное значение, при котором плагин показывает target.
-- Общий способ для чужих плагинов: шкалу параметра знает только сам плагин.
local function seek(tr, fx, p, target)
  local before = r.TrackFX_GetParamNormalized(tr, fx, p)
  local function at(x) return (parse_number(text_at(tr, fx, p, x))) end
  local function give_up(msg)
    r.TrackFX_SetParamNormalized(tr, fx, p, before)
    fail(msg)
  end
  local want, unit = parse_number(target)
  if not want then fail('цель не число: ' .. tostring(target)) end
  local t_lo, t_hi = text_at(tr, fx, p, 0), text_at(tr, fx, p, 1)
  local v_lo, u_lo = parse_number(t_lo)
  local v_hi, u_hi = parse_number(t_hi)
  local name = param_name(tr, fx, p)
  if not v_lo or not v_hi then
    give_up('значение параметра «' .. name .. '» не читается как число — ставь через text')
  end
  local scale = 'шкала параметра «' .. name .. '» — от «' .. t_lo .. '» до «' .. t_hi .. '»'
  -- Цель без единицы на шкале в миллисекундах — миллисекунды: 12 значит 12 мс, а не 12 с.
  -- Шкала, где миллисекунды переходят в секунды, двусмысленна: единицу называет вызывающий.
  if unit == '' and (u_lo == 'ms' or u_hi == 'ms') then
    if SECONDS[u_lo] or SECONDS[u_hi] then
      give_up('цель ' .. tostring(target) .. ' без единицы, а ' .. scale .. ': укажи её — {fmt = "12 ms"} или {fmt = "1.5 s"}')
    end
    want = want / 1000
  end
  -- Цель за краем шкалы — почти всегда не та единица. Молча встать на край нельзя:
  -- так ревер получает предзадержку 500 мс вместо 12.
  local low, high = math.min(v_lo, v_hi), math.max(v_lo, v_hi)
  local span = high - low
  local slack = (span == math.huge or span ~= span) and 0 or span * 0.01
  if want < low - slack or want > high + slack then
    give_up('цель ' .. tostring(target) .. ' вне шкалы: ' .. scale .. ' — проверь единицу')
  end
  if v_lo == want or v_hi == want then
    set_norm(tr, fx, p, v_lo == want and 0 or 1)
    return
  end
  local rising = v_hi >= v_lo
  -- Место на шкале, где показание перестаёт быть «до цели»: слева before_target(v) истинно.
  local function flip(before_target)
    local lo, hi = 0, 1
    for _ = 1, 40 do
      local mid = (lo + hi) / 2
      local v = at(mid)
      if not v then break end
      if before_target(v) then lo = mid else hi = mid end
    end
    return lo, hi
  end
  local lo, first = flip(function(v) if rising then return v < want end return v > want end)
  if at(first) == want then
    -- Показание округлено: цель показывает целый отрезок шкалы. Его середина — значение,
    -- которое плагин и покажет, и применит; край отрезка отличался бы на полшага округления.
    local last = flip(function(v) if rising then return v <= want end return v >= want end)
    set_norm(tr, fx, p, (first + math.max(first, last)) / 2)
    return
  end
  -- Ровно такого показания нет (ступенчатый параметр): ближайшее из соседних.
  local e_lo = math.abs((at(lo) or math.huge) - want)
  local e_hi = math.abs((at(first) or math.huge) - want)
  set_norm(tr, fx, p, e_lo <= e_hi and lo or first)
end

-- Варианты параметра-переключателя: проходит весь диапазон и собирает разные показания.
local function scan_options(tr, fx, p)
  local before = r.TrackFX_GetParamNormalized(tr, fx, p)
  local options, by_text = A {}, {}
  for i = 0, 400 do
    local x = i / 400
    local text = text_at(tr, fx, p, x)
    local opt = by_text[text]
    if not opt then
      opt = { name = text, label = text, first = x }
      by_text[text] = opt
      options[#options + 1] = opt
    end
    opt.last = x
  end
  r.TrackFX_SetParamNormalized(tr, fx, p, before)
  for _, opt in ipairs(options) do opt.norm = (opt.first + opt.last) / 2 end
  return options
end

-- Список вариантов текстового параметра («Bell», «Low Cut», …) с их значениями 0..1.
function rx.fx_options(track, fxref, pref)
  local tr, fx = rx.fx(track, fxref)
  local p = rx.param(tr, fx, pref)
  local options = rx.scratch(function() return scan_options(tr, fx, p) end)
  local out = A {}
  for _, opt in ipairs(options) do out[#out + 1] = { text = opt.name, norm = opt.norm } end
  return out
end

-- value: число — нормализованное 0..1; {fmt = '90'} — число, как его показывает плагин;
-- {text = 'Low Cut'} — вариант переключателя по названию; {raw = x} — в родных единицах.
function rx.fx_set(track, fxref, pref, value)
  local tr, fx = rx.fx(track, fxref)
  local p = rx.param(tr, fx, pref)
  if type(value) == 'number' then set_norm(tr, fx, p, value)
  elseif value.raw then r.TrackFX_SetParam(tr, fx, p, value.raw)
  elseif value.fmt then seek(tr, fx, p, value.fmt)
  elseif value.text then
    local options = scan_options(tr, fx, p)
    if #options > 60 then fail('параметр «' .. param_name(tr, fx, p) .. '» не переключатель — ставь через fmt') end
    set_norm(tr, fx, p, pick(options, value.text, 'вариант').norm)
  else fail('значение: число 0..1, {fmt = ...}, {text = ...} или {raw = ...}') end
  local info = param_info(tr, fx, p)
  -- Некоторые форматы (CLAP) обновляют показание с задержкой: берём его по значению.
  info.fmt = text_at(tr, fx, p, info.norm)
  return info
end

-- name — как в каталоге плагинов (колонка add); pos — место в цепочке (с 1), по умолчанию в конец.
function rx.fx_add(track, name, pos)
  local tr = rx.track(track)
  local fx = r.TrackFX_AddByName(tr, name, false, pos and (-1000 - (pos - 1)) or -1)
  if fx < 0 then fail('плагин не добавился: ' .. name) end
  local _, actual = r.TrackFX_GetFXName(tr, fx, '')
  return { fx = fx + 1, name = actual, params = r.TrackFX_GetNumParams(tr, fx) }
end

function rx.fx_remove(track, fxref)
  local tr, fx = rx.fx(track, fxref)
  r.TrackFX_Delete(tr, fx)
  return fx_list(tr)
end

function rx.fx_enable(track, fxref, on)
  local tr, fx = rx.fx(track, fxref)
  r.TrackFX_SetEnabled(tr, fx, on)
  return fx_list(tr)
end

-- ── Посылы ────────────────────────────────────────────────────────────────

-- props: vol_db, pan, mute, mode (0 — после фейдера, 1 — до плагинов, 3 — до фейдера).
function rx.send_add(src, dst, props)
  local tr = rx.track(src)
  local i = r.CreateTrackSend(tr, rx.track(dst))
  if i < 0 then fail('посыл не создан') end
  for k, v in pairs(props or {}) do
    if k == 'vol_db' then r.SetTrackSendInfo_Value(tr, 0, i, 'D_VOL', db_to_gain(v))
    elseif k == 'pan' then r.SetTrackSendInfo_Value(tr, 0, i, 'D_PAN', v)
    elseif k == 'mute' then r.SetTrackSendInfo_Value(tr, 0, i, 'B_MUTE', v and 1 or 0)
    elseif k == 'mode' then r.SetTrackSendInfo_Value(tr, 0, i, 'I_SENDMODE', v)
    else fail('неизвестное свойство посыла: ' .. tostring(k)) end
  end
  return send_list(tr)
end

-- Запоминает выделение треков, выполняет fn и возвращает выделение.
local function keeping_selection(fn)
  local selected = {}
  for i = 0, r.CountSelectedTracks(0) - 1 do selected[#selected + 1] = r.GetSelectedTrack(0, i) end
  local ok, res = pcall(fn)
  for i = 0, r.CountTracks(0) - 1 do r.SetTrackSelected(r.GetTrack(0, i), false) end
  for _, tr in ipairs(selected) do
    if r.ValidatePtr2(0, tr, 'MediaTrack*') then r.SetTrackSelected(tr, true) end
  end
  if not ok then error(res, 0) end
  return res
end

-- Треки по списку ссылок, в порядке их расположения в проекте.
local function track_list(refs)
  local list = {}
  for _, ref in ipairs(refs) do list[#list + 1] = rx.track(ref) end
  table.sort(list, function(a, b) return track_number(a) < track_number(b) end)
  if #list == 0 then fail('список треков пуст') end
  return list
end

-- Новый трек. opts.after — после какого трека вставить (по умолчанию в конец).
-- После трека-папки новый трек становится её первой дорожкой.
function rx.track_add(name, opts)
  local idx = opts and opts.after and track_number(rx.track(opts.after)) or r.CountTracks(0)
  r.InsertTrackAtIndex(idx, true)
  local tr = r.GetTrack(0, idx)
  r.GetSetMediaTrackInfo_String(tr, 'P_NAME', name, true)
  return track_info(tr)
end

-- Папка: новый трек-родитель, перечисленные треки переезжают под него и звучат через него.
function rx.folder(name, refs)
  local list = track_list(refs)
  local first = track_number(list[1])
  r.InsertTrackAtIndex(first - 1, true)
  local parent = r.GetTrack(0, first - 1)
  r.GetSetMediaTrackInfo_String(parent, 'P_NAME', name, true)
  keeping_selection(function()
    for i, tr in ipairs(list) do
      if i == 1 then r.SetOnlyTrackSelected(tr) else r.SetTrackSelected(tr, true) end
    end
    r.ReorderSelectedTracks(first, 1)
  end)
  return track_info(parent)
end

-- Шина посылами: новый трек после перечисленных; они шлют в него и перестают идти в мастер.
function rx.bus(name, refs)
  local list = track_list(refs)
  local bus = rx.track(rx.track_add(name, { after = list[#list] }).guid)
  for _, tr in ipairs(list) do
    r.SetMediaTrackInfo_Value(tr, 'B_MAINSEND', 0)
    r.CreateTrackSend(tr, bus)
  end
  return track_info(bus)
end

-- Сайдчейн: сигнал src приходит на каналы 3/4 трека dst (до фейдера src), откуда его
-- берёт вход сайдчейна плагина. Возвращает параметры плагина, похожие на переключатель
-- сайдчейна: включить внешний вход нужно в самом плагине. У ReaComp включается сразу.
function rx.sidechain(src, dst, fxref)
  local s, d = rx.track(src), rx.track(dst)
  if r.GetMediaTrackInfo_Value(d, 'I_NCHAN') < 4 then r.SetMediaTrackInfo_Value(d, 'I_NCHAN', 4) end
  local send
  for i = 0, r.GetTrackNumSends(s, 0) - 1 do
    local ok, same = pcall(function() return r.GetTrackSendInfo_Value(s, 0, i, 'P_DESTTRACK') == d end)
    if ok and same and r.GetTrackSendInfo_Value(s, 0, i, 'I_DSTCHAN') == 2 then send = i end
  end
  if not send then
    send = r.CreateTrackSend(s, d)
    r.SetTrackSendInfo_Value(s, 0, send, 'I_DSTCHAN', 2)
    r.SetTrackSendInfo_Value(s, 0, send, 'I_SENDMODE', 3)
    r.SetTrackSendInfo_Value(s, 0, send, 'D_VOL', 1)
  end
  local result = { send = send + 1, channels = math.floor(r.GetMediaTrackInfo_Value(d, 'I_NCHAN')) }
  if not fxref then return result end

  local _, fx = rx.fx(d, fxref)
  local _, name = r.TrackFX_GetFXName(d, fx, '')
  local _, inputs = r.TrackFX_GetIOSize(d, fx)
  result.plugin_inputs = inputs
  if inputs < 4 then result.note = 'у плагина нет входа сайдчейна (входов меньше четырёх)' end
  if name:find('ReaComp', 1, true) or name:find('ReaXcomp', 1, true) or name:find('ReaGate', 1, true) then
    -- У плагинов Cockos вход детектора — число: 2 из 1084 — вспомогательный вход L+R.
    r.TrackFX_SetParam(d, fx, rx.param(d, fx, 'SignIn'), 2 / 1084)
    result.enabled = true
    return result
  end
  local hints = A {}
  for p = 0, r.TrackFX_GetNumParams(d, fx) - 1 do
    local pname = param_name(d, fx, p):lower()
    if #hints < 12 and (pname:find('side') or pname:find('sidechain') or pname:find('ext') or pname:find('key') or pname:find('detector')) then
      hints[#hints + 1] = p .. ' ' .. param_name(d, fx, p) .. ' = ' .. param_fmt(d, fx, p)
    end
  end
  result.switch_candidates = hints
  return result
end

-- ── Прослушивание и история ───────────────────────────────────────────────

-- Запускает воспроизведение. range = {t0, t1} — этот участок по кругу (loop = false — один раз).
function rx.play(range, loop)
  if range then
    r.GetSet_LoopTimeRange2(0, true, true, range[1], range[2], false)
    r.GetSetRepeat(loop == false and 0 or 1)
    r.SetEditCurPos(range[1], true, true)
  end
  r.OnPlayButton()
  return { playing = r.GetPlayState() & 1 == 1, position = r.GetPlayPosition() }
end

function rx.stop()
  r.OnStopButton()
  return { playing = false }
end

-- Вершины истории undo и redo.
function rx.history()
  return { undo = r.Undo_CanUndo2(0) or '', redo = r.Undo_CanRedo2(0) or '' }
end

-- Переключает последнюю правку: 'before' — как было до неё, 'after' — с ней,
-- без аргумента — в другое состояние. Идёт через undo/redo, поэтому мгновенно и точно.
function rx.ab(want)
  local preview = rx.preview()
  if preview then
    if want == 'before' then return { state = 'before', label = preview } end
    r.Undo_DoRedo2(0)
    r.SetExtState('reamix', 'preview', '', false)
    return { state = 'after', label = preview }
  end
  local top = r.Undo_CanUndo2(0)
  if not is_edit(top) then fail('последнее действие в истории — не правка reamix: ' .. tostring(top)) end
  if want == 'after' then return { state = 'after', label = top } end
  r.Undo_DoUndo2(0)
  r.SetExtState('reamix', 'preview', top, false)
  return { state = 'before', label = top }
end

-- Откатывает последнюю правку reamix. Если её уже слушали в состоянии «до» —
-- оставляет как есть: откат состоялся.
function rx.undo_edit()
  local preview = rx.preview()
  if preview then
    r.SetExtState('reamix', 'preview', '', false)
    return preview
  end
  local top = r.Undo_CanUndo2(0)
  if not is_edit(top) then fail('последнее действие в истории — не правка reamix: ' .. tostring(top)) end
  r.Undo_DoUndo2(0)
  return top
end

-- ── Огибающие ─────────────────────────────────────────────────────────────

local TRACK_ENVELOPES = { volume = '<VOLENV2', pan = '<PANENV2', mute = '<MUTEENV' }

local function env_flag(env, key, value)
  local _, current = r.GetSetEnvelopeInfo_String(env, key, value or '', value ~= nil)
  return current
end

-- target: 'volume' | 'pan' | 'mute' | {fx = ..., param = ...}. create — завести, если нет.
-- Встроенные огибающие у трека есть всегда, но выключенные: «завести» значит включить
-- и показать. При включении громкости REAPER переносит уровень фейдера в огибающую и
-- ставит фейдер в 0 дБ — дальше фейдер работает как трим поверх огибающей.
function rx.envelope(track, target, create)
  local tr = rx.track(track)
  if type(target) == 'table' then
    local _, fx = rx.fx(tr, target.fx)
    local p = rx.param(tr, fx, target.param)
    return r.GetFXEnvelope(tr, fx, p, create or false) or fail('у параметра нет огибающей'), tr
  end
  local chunk = TRACK_ENVELOPES[target] or fail('неизвестная огибающая: ' .. tostring(target))
  local env = r.GetTrackEnvelopeByChunkName(tr, chunk) or fail('огибающей нет: ' .. target)
  if env_flag(env, 'ACTIVE') ~= '1' then
    if not create then fail('огибающая выключена: ' .. target) end
    env_flag(env, 'ACTIVE', '1')
    env_flag(env, 'VISIBLE', '1')
  end
  return env, tr
end

local function lines_of(path)
  local f = io.open(path, 'r') or fail('файл не открывается: ' .. path)
  local text = f:read('a')
  f:close()
  return text:gmatch('[^\r\n]+')
end

-- Точки огибающей со всеми полями. path — записать их в файл (TSV) мимо ответа,
-- в ответе останется только сводка.
function rx.env_read(track, target, path)
  local env = rx.envelope(track, target)
  local mode = r.GetEnvelopeScalingMode(env)
  local points, lo, hi = A {}, nil, nil
  for i = 0, r.CountEnvelopePoints(env) - 1 do
    local _, time, value, shape, tension = r.GetEnvelopePoint(env, i)
    local v = r.ScaleFromEnvelopeMode(mode, value)
    if not lo or v < lo then lo = v end
    if not hi or v > hi then hi = v end
    points[#points + 1] = { t = time, v = v, shape = shape, tension = tension }
  end
  local summary = {
    points = #points, min = lo, max = hi,
    start = points[1] and points[1].t, stop = points[#points] and points[#points].t,
    auto_items = r.CountAutomationItems(env),
  }
  if not path then summary.list = points return summary end
  local f = assert(io.open(path, 'w'))
  f:write('t\tv\tshape\ttension\n')
  for _, pt in ipairs(points) do
    f:write(string.format('%.17g\t%.17g\t%d\t%.17g\n', pt.t, pt.v, pt.shape, pt.tension))
  end
  f:close()
  summary.file = path
  return summary
end

-- Линейная интерполяция v по списку точек, отсортированному по t.
local function interpolate(points, t)
  if t <= points[1].t then return points[1].v end
  for i = 2, #points do
    local a, b = points[i - 1], points[i]
    if t <= b.t then
      if b.t == a.t then return b.v end
      return a.v + (b.v - a.v) * (t - a.t) / (b.t - a.t)
    end
  end
  return points[#points].v
end

-- Пишет точки в огибающую. points — список {t, v, shape?, tension?} либо путь к
-- TSV-файлу такого же вида (как пишет env_read). opts.db — значения в дБ (громкость);
-- opts.clear = {t0, t1} — сначала убрать точки в диапазоне; по умолчанию — в
-- диапазоне новых точек. opts.add — значения не абсолютные, а поправка к уже
-- нарисованной кривой: старые точки в диапазоне сохраняют форму и сдвигаются.
function rx.env_write(track, target, points, opts)
  opts = opts or {}
  local env = rx.envelope(track, target, true)
  if type(points) == 'string' then
    local list = {}
    for line in lines_of(points) do
      local t, v, shape, tension = line:match('^(%S+)\t(%S+)\t?(%S*)\t?(%S*)')
      if tonumber(t) then
        list[#list + 1] = { t = tonumber(t), v = tonumber(v), shape = tonumber(shape), tension = tonumber(tension) }
      end
    end
    points = list
  end
  if #points == 0 then fail('нет точек') end
  local t0, t1 = points[1].t, points[1].t
  for _, pt in ipairs(points) do
    if pt.t < t0 then t0 = pt.t end
    if pt.t > t1 then t1 = pt.t end
  end
  local clear = opts.clear or { t0, t1 }
  local mode = r.GetEnvelopeScalingMode(env)
  if opts.add then
    table.sort(points, function(a, b) return a.t < b.t end)
    -- Поправка нужна и в местах старых точек — иначе их форма внутри диапазона потеряется.
    local merged = {}
    for _, pt in ipairs(points) do merged[#merged + 1] = { t = pt.t, v = pt.v, shape = pt.shape, tension = pt.tension } end
    for i = 0, r.CountEnvelopePoints(env) - 1 do
      local _, time, _, shape, tension = r.GetEnvelopePoint(env, i)
      if time > clear[1] and time < clear[2] then
        merged[#merged + 1] = { t = time, v = interpolate(points, time), shape = shape, tension = tension }
      end
    end
    local srate = rx.project().srate
    for _, pt in ipairs(merged) do
      local _, raw = r.Envelope_Evaluate(env, pt.t, srate, 1)
      local base = r.ScaleFromEnvelopeMode(mode, raw)
      pt.v = opts.db and base * db_to_gain(pt.v) or base + pt.v
    end
    points = merged
  end
  r.DeleteEnvelopePointRange(env, clear[1] - 1e-9, clear[2] + 1e-9)
  for _, pt in ipairs(points) do
    local v = (opts.db and not opts.add) and db_to_gain(pt.v) or pt.v
    r.InsertEnvelopePoint(env, pt.t, r.ScaleToEnvelopeMode(mode, v), pt.shape or 0, pt.tension or 0, false, true)
  end
  r.Envelope_SortPoints(env)
  return { points = r.CountEnvelopePoints(env), written = #points, from = t0, to = t1 }
end

-- ── Рендер ────────────────────────────────────────────────────────────────

local RENDER_NUM = {
  'RENDER_SETTINGS', 'RENDER_BOUNDSFLAG', 'RENDER_CHANNELS', 'RENDER_SRATE', 'RENDER_STARTPOS',
  'RENDER_ENDPOS', 'RENDER_TAILFLAG', 'RENDER_ADDTOPROJ', 'RENDER_DITHER', 'RENDER_NORMALIZE',
}
local RENDER_STR = { 'RENDER_FILE', 'RENDER_PATTERN', 'RENDER_FORMAT', 'RENDER_FORMAT2' }

local function path_exists(path)
  local f = io.open(path, 'r')
  if f then f:close() end
  return f ~= nil
end

-- Файлы источников, которые REAPER не смог открыть.
local function offline_sources()
  local out = A {}
  for i = 0, r.CountMediaItems(0) - 1 do
    local take = r.GetActiveTake(r.GetMediaItem(0, i))
    local src = take and r.GetMediaItemTake_Source(take)
    if src and r.GetMediaSourceType(src, '') ~= 'MIDI' and r.GetMediaSourceLength(src) == 0 then
      out[#out + 1] = r.GetMediaSourceFileName(src, '')
    end
  end
  return out
end

-- Папка записи проекта. Считаем сами: GetProjectPath создаёт её при вызове.
local function record_dir()
  local _, rec = r.GetSetProjectInfo_String(0, 'RECORD_PATH', '', false)
  local _, proj = r.EnumProjects(-1)
  local dir = proj and proj:match('^(.*)[/\\][^/\\]*$')
  if rec == '' or not dir then return nil end
  return rec:sub(1, 1) == '/' and rec or dir .. '/' .. rec
end

-- Офлайн-рендер в папку opts.dir. opts.tracks — список треков для стемов (после плагинов
-- и фейдера; с opts.prefader — до фейдера и панорамы); opts.mix — ещё и общий микс;
-- opts.range = {t0, t1} — участок, иначе весь проект.
-- Настройки рендера проекта и выделение треков возвращаются как были.
-- Solo — состояние прослушивания, а не микса: с ним «микс» вышел бы из одних солированных
-- дорожек, а стемы остальных — пустыми. На время рендера оно снимается.
function rx.render(opts)
  local saved_num, saved_str, selected = {}, {}, {}
  for _, k in ipairs(RENDER_NUM) do saved_num[k] = r.GetSetProjectInfo(0, k, 0, false) end
  for _, k in ipairs(RENDER_STR) do
    local _, v = r.GetSetProjectInfo_String(0, k, '', false)
    saved_str[k] = v
  end
  for i = 0, r.CountSelectedTracks(0) - 1 do selected[#selected + 1] = r.GetSelectedTrack(0, i) end
  local soloed, unsoloed = {}, A {}
  for i = 0, r.CountTracks(0) - 1 do
    local tr = r.GetTrack(0, i)
    local mode = r.GetMediaTrackInfo_Value(tr, 'I_SOLO')
    if mode ~= 0 then
      soloed[#soloed + 1] = { tr = tr, mode = mode }
      unsoloed[#unsoloed + 1] = (i + 1) .. ' ' .. track_name(tr)
      r.SetMediaTrackInfo_Value(tr, 'I_SOLO', 0)
    end
  end

  local function num(k, v) r.GetSetProjectInfo(0, k, v, true) end
  local function str(k, v) r.GetSetProjectInfo_String(0, k, v, true) end
  local files = A {}

  local function pass(settings, pattern)
    num('RENDER_SETTINGS', settings)
    str('RENDER_PATTERN', pattern)
    local _, targets = r.GetSetProjectInfo_String(0, 'RENDER_TARGETS', '', false)
    r.Main_OnCommand(42230, 0)  -- рендер с текущими настройками, окно закрывается само
    for path in targets:gmatch('[^;]+') do files[#files + 1] = path end
  end

  -- Пока REAPER неактивен, он может держать медиа отключёнными (настройка «Set media
  -- items offline when application is not active») — тогда рендер выходит пустым.
  r.Main_OnCommand(40101, 0)  -- Item: Set all media online
  -- При рендере REAPER создаёт папку записи проекта; если её не было — уберём пустую.
  local media_dir = record_dir()
  local had_media_dir = media_dir and path_exists(media_dir)

  local ok, err = pcall(function()
    r.RecursiveCreateDirectory(opts.dir, 0)
    str('RENDER_FILE', opts.dir)
    str('RENDER_FORMAT', opts.format or 'ZXZhdyAAAA==')
    str('RENDER_FORMAT2', '')
    num('RENDER_CHANNELS', 2)
    num('RENDER_SRATE', 0)
    num('RENDER_TAILFLAG', 0)
    num('RENDER_ADDTOPROJ', 0)
    num('RENDER_DITHER', 0)
    num('RENDER_NORMALIZE', 0)
    if opts.range then
      num('RENDER_BOUNDSFLAG', 0)
      num('RENDER_STARTPOS', opts.range[1])
      num('RENDER_ENDPOS', opts.range[2])
    else
      num('RENDER_BOUNDSFLAG', 1)
    end
    if opts.mix then pass(0, 'mix') end
    if opts.tracks and #opts.tracks > 0 then
      for i, ref in ipairs(opts.tracks) do
        local tr = rx.track(ref)
        if i == 1 then r.SetOnlyTrackSelected(tr) else r.SetTrackSelected(tr, true) end
      end
      pass(3 + (opts.prefader and 8192 or 0), 'stem-$tracknumber')
    end
  end)

  for _, k in ipairs(RENDER_NUM) do num(k, saved_num[k]) end
  for _, k in ipairs(RENDER_STR) do str(k, saved_str[k]) end
  for i = 0, r.CountTracks(0) - 1 do r.SetTrackSelected(r.GetTrack(0, i), false) end
  r.SetTrackSelected(r.GetMasterTrack(0), false)
  for _, tr in ipairs(selected) do r.SetTrackSelected(tr, true) end
  for _, s in ipairs(soloed) do r.SetMediaTrackInfo_Value(s.tr, 'I_SOLO', s.mode) end
  if media_dir and not had_media_dir then os.remove(media_dir) end
  if not ok then error(err, 0) end
  return {
    dir = opts.dir, files = files, offline = offline_sources(), unsoloed = unsoloed,
    rev = r.GetProjectStateChangeCount(0),
  }
end

-- ── Гейн-стейджинг ────────────────────────────────────────────────────────

local TRIM = 'JS:utility/volume'

-- Трим — «JS: Volume Adjustment» первым плагином дорожки.
local function has_trim(tr)
  if r.TrackFX_GetCount(tr) == 0 then return false end
  local _, ident = r.TrackFX_GetNamedConfigParm(tr, 0, 'fx_ident')
  return ident == 'utility/volume'
end

local function fx_active(tr, fx)
  return r.TrackFX_GetEnabled(tr, fx) and not r.TrackFX_GetOffline(tr, fx)
end

-- Ставит трим первым плагином (или берёт уже стоящий) и задаёт поправку в дБ.
function rx.trim(track, db)
  local tr = rx.track(track)
  if not has_trim(tr) then rx.fx_add(tr, TRIM, 1) end
  r.TrackFX_SetEnabled(tr, 0, true)
  return rx.fx_set(tr, 1, 0, { raw = db })
end

-- Стемы до фейдера на каждой ступени цепочки плагинов трека: вход (все плагины выключены),
-- затем после каждого работающего плагина. Плагины выключаются только на время рендера.
function rx.chain_render(track, dir, range)
  local tr = rx.track(track)
  local n = r.TrackFX_GetCount(tr)
  local on, off = {}, A {}
  for fx = 0, n - 1 do
    on[fx] = fx_active(tr, fx)
    if not on[fx] then
      local _, name = r.TrackFX_GetFXName(tr, fx, '')
      off[#off + 1] = { fx = fx + 1, name = name }
    end
  end
  local steps = rx.scratch(function()
    local steps = A {}
    for k = 0, n do
      if k == 0 or on[k - 1] then
        for fx = 0, n - 1 do r.TrackFX_SetEnabled(tr, fx, on[fx] and fx < k) end
        local res = rx.render({ dir = dir .. '/' .. k, tracks = { tr }, range = range, prefader = true })
        local name = ''
        if k > 0 then _, name = r.TrackFX_GetFXName(tr, k - 1, '') end
        steps[#steps + 1] = { fx = k, name = name, file = res.files[1] }
      end
    end
    return steps
  end)
  return { steps = steps, off = off, trim = has_trim(tr) and on[0] }
end

-- Стемы треков до плагинов и фейдера — уровень источника, под который ставится трим.
-- Про каждый трек: поправка уже стоящего трима и остальные работающие плагины.
function rx.input_render(tracks, dir, range)
  local list, info = {}, A {}
  for _, ref in ipairs(tracks) do
    local tr = rx.track(ref)
    local trim, others = has_trim(tr), A {}
    for fx = trim and 1 or 0, r.TrackFX_GetCount(tr) - 1 do
      if fx_active(tr, fx) then
        local _, name = r.TrackFX_GetFXName(tr, fx, '')
        others[#others + 1] = name
      end
    end
    list[#list + 1] = tr
    info[#info + 1] = { n = track_number(tr), trim = trim and r.TrackFX_GetParam(tr, 0, 0) or nil, others = others }
  end
  local res = rx.scratch(function()
    for _, tr in ipairs(list) do
      for fx = 0, r.TrackFX_GetCount(tr) - 1 do r.TrackFX_SetEnabled(tr, fx, false) end
    end
    return rx.render({ dir = dir, tracks = list, range = range, prefader = true })
  end)
  return { files = res.files, tracks = info }
end

-- ── Снимки ────────────────────────────────────────────────────────────────

local function write_file(path, text)
  local f = assert(io.open(path, 'wb'))
  f:write(text)
  f:close()
end

-- У инструментов бывают десятки тысяч параметров; для списка изменений хватит первых.
local PARAMS_KEPT = 2048

-- Значения параметров плагинов трека: строка на плагин — GUID и числа в родных единицах
-- параметра. По ним список изменений называет, какую ручку повернули: в чанке состояние
-- плагина непрозрачно. Нормализованное значение не годится: у AU шкала параметра зависит
-- от частоты дискретизации, и то же значение читается по-разному. Параметр под активной
-- огибающей пишется как «~»: им управляет автоматизация, а не ручка.
local function params_text(tr)
  local automated = {}
  for i = 0, r.CountTrackEnvelopes(tr) - 1 do
    local env = r.GetTrackEnvelope(tr, i)
    local _, fx, p = r.Envelope_GetParentTrack(env)
    local _, active = r.GetSetEnvelopeInfo_String(env, 'ACTIVE', '', false)
    if fx and fx >= 0 and active == '1' then automated[fx .. ':' .. p] = true end
  end
  local lines = {}
  for fx = 0, r.TrackFX_GetCount(tr) - 1 do
    local values = {}
    for p = 0, math.min(r.TrackFX_GetNumParams(tr, fx), PARAMS_KEPT) - 1 do
      values[#values + 1] = automated[fx .. ':' .. p] and '~' or string.format('%.9g', (r.TrackFX_GetParam(tr, fx, p)))
    end
    lines[#lines + 1] = r.TrackFX_GetFXGUID(tr, fx) .. '\t' .. table.concat(values, ' ')
  end
  return table.concat(lines, '\n')
end

-- Полное состояние каждого трека родными чанками REAPER плюс скелет — в папку dir.
function rx.snapshot(dir)
  r.RecursiveCreateDirectory(dir, 0)
  local index = A {}
  local function dump(tr)
    local n = track_number(tr)
    local ok, chunk = r.GetTrackStateChunk(tr, '', false)
    if not ok then fail('не читается состояние трека №' .. n) end
    local file = string.format('%03d.rtrack', n)
    write_file(dir .. '/' .. file, chunk)
    local params = params_text(tr)
    if params ~= '' then write_file(dir .. '/' .. string.format('%03d.params', n), params) end
    index[#index + 1] = { n = n, guid = r.GetTrackGUID(tr), name = track_name(tr), file = file, bytes = #chunk }
  end
  dump(r.GetMasterTrack(0))
  for i = 0, r.CountTracks(0) - 1 do dump(r.GetTrack(0, i)) end
  write_file(dir .. '/skeleton.json', json.encode(rx.skeleton()))
  return { dir = dir, rev = r.GetProjectStateChangeCount(0), tracks = index }
end

local function track_by_guid(guid)
  local master = r.GetMasterTrack(0)
  if r.GetTrackGUID(master) == guid then return master end
  for i = 0, r.CountTracks(0) - 1 do
    local tr = r.GetTrack(0, i)
    if r.GetTrackGUID(tr) == guid then return tr end
  end
end

-- Для списка изменений: имена сдвинувшихся параметров и их показания — было и стало.
-- list — { {track = guid, fx = guid, params = { {p, было, стало}, … }}, … }, значения в
-- родных единицах; ответ в том же порядке. Показание берётся по значению, параметр при
-- этом не трогается; плагин, который так не умеет, получает в ответ сами числа.
function rx.params_describe(list)
  local out = A {}
  for _, item in ipairs(list) do
    local entry = { params = A {} }
    local tr = track_by_guid(item.track)
    for fx = 0, (tr and r.TrackFX_GetCount(tr) or 0) - 1 do
      if r.TrackFX_GetFXGUID(tr, fx) == item.fx then
        local _, name = r.TrackFX_GetFXName(tr, fx, '')
        entry.i, entry.name = fx + 1, name
        for _, move in ipairs(item.params) do
          local p = move[1]
          local _, lo, hi = r.TrackFX_GetParam(tr, fx, p)
          local function shown(raw)
            local ok, text = r.TrackFX_FormatParamValueNormalized(tr, fx, p, hi > lo and (raw - lo) / (hi - lo) or raw, '')
            return ok and text ~= '' and text or string.format('%.4g', raw)
          end
          entry.params[#entry.params + 1] = { name = param_name(tr, fx, p), was = shown(move[2]), now = shown(move[3]) }
        end
      end
    end
    out[#out + 1] = entry
  end
  return out
end

-- Возвращает треку состояние из файла чанка.
function rx.restore_track(track, path)
  local tr = rx.track(track)
  local f = io.open(path, 'rb') or fail('файл не открывается: ' .. path)
  local chunk = f:read('a')
  f:close()
  if not r.SetTrackStateChunk(tr, chunk, false) then fail('REAPER не принял состояние трека') end
  return track_info(tr)
end

return rx
