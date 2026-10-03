-- Подделка API REAPER для тестов моста и lib.lua вне REAPER.
-- Моделирует ровно то, что используют bridge/*.lua: треки, плагины с параметрами,
-- историю undo, ExtState и defer. Реальное поведение API проверяется только вживую.

local F = { rev = 100, ext = {}, undo = {}, redo = {}, renders = {}, audio = 1, audio_log = {}, path = '/music/song/main.rpp' }

local function hz(norm) return string.format('%.1f Hz', 20 * 1000 ^ norm) end
local function khz(norm)
  local f = 20 * 1000 ^ norm
  if f >= 1000 then return string.format('%.2f kHz', f / 1000) end
  return string.format('%.0f Hz', f)
end
local function gain_db(norm) return string.format('%.1f dB', norm * 48 - 24) end
local function ratio(norm) return string.format('%.1f:1', 1 + norm * 19) end

local guid_seq = 0
local function guid()
  guid_seq = guid_seq + 1
  return string.format('{00000000-0000-0000-0000-%012d}', guid_seq)
end

local function fx(name, params)
  return { name = name, guid = guid(), enabled = true, offline = false, params = params }
end

local function track(name, depth, plugins)
  return {
    name = name, guid = guid(), depth = depth or 0, fx = plugins or {},
    vals = {
      D_VOL = 1, D_PAN = 0, D_WIDTH = 1, B_MUTE = 0, I_SOLO = 0, B_PHASE = 0,
      B_MAINSEND = 1, I_NCHAN = 2, I_AUTOMODE = 0, I_FOLDERDEPTH = 0,
      I_PLAY_OFFSET_FLAG = 1, D_PLAY_OFFSET = 0,
    },
  }
end

-- Установленные плагины: имя → чистый экземпляр с параметрами по умолчанию.
local catalog = {
  ['VST: ReaEQ (Cockos)'] = function()
    return {
      { name = 'Freq-Band 1', norm = 0.5, fmt = hz },
      { name = 'Gain-Band 1', norm = 0.5, fmt = gain_db },
    }
  end,
  ['VST3: Pro-C 2 (FabFilter)'] = function()
    return {
      { name = 'Ratio', norm = 0.2, fmt = ratio },
      { name = 'Sidechain HP', norm = 0.1, fmt = khz },
      { name = 'Style', norm = 0, fmt = function(norm) return ({ 'Clean', 'Punch', 'Vocal' })[math.min(3, math.floor(norm * 3) + 1)] end },
    }
  end,
  ['VST: ReaDelay (Cockos)'] = function()
    return {
      { name = 'Predelay', norm = 0, fmt = function(norm) return string.format('%.1f ms', norm * 500) end },
      -- Шкала, на которой миллисекунды переходят в секунды.
      { name = 'Length', norm = 0, fmt = function(norm)
        local ms = 1 + norm * 1999
        if ms >= 1000 then return string.format('%.2f s', ms / 1000) end
        return string.format('%.1f ms', ms)
      end },
      -- Ступени, как у Pultec: шкала целиком в килогерцах.
      { name = 'High Freq', norm = 0, fmt = function(norm)
        local steps = { 3, 4, 5, 8, 10, 12, 16 }
        return steps[math.min(#steps, math.floor(norm * #steps) + 1)] .. ' KCS'
      end },
    }
  end,
  -- Как Waves: новое значение показывает сразу, а в состояние (то, что попадёт в чанк
  -- и в точку undo) переносит только в аудиопотоке.
  ['VST3: RVox Stereo (Waves)'] = function()
    return { { name = 'Compression', norm = 1, fmt = function(norm) return string.format('%.1f dB', norm * 36 - 36) end } }
  end,
  -- То же, но аудиопоток до плагина не доходит: состояние не обновляется никогда.
  ['VST3: NLS Channel Stereo (Waves)'] = function()
    return { { name = 'Drive', norm = 0.5, fmt = function(norm) return string.format('%.1f', norm * 10) end } }
  end,
  ['JS: utility/volume'] = function()
    return { { name = 'Adjustment (dB)', norm = 0, fmt = function(v) return string.format('%.1f', v) end } }
  end,
}

local function live_state(plugin)
  local values = {}
  for _, param in ipairs(plugin.params) do values[#values + 1] = string.format('%s %.17g', param.name, param.norm) end
  return table.concat(values, ';')
end

local function installed(name)
  local full = name:gsub('^(%w+): ?', '%1: ')
  local plugin = fx(full, catalog[full] and catalog[full]() or {})
  if full:find('Waves') then
    plugin.lazy, plugin.saved = true, live_state(plugin)
    plugin.deaf = full:find('NLS') ~= nil
  end
  return plugin
end

F.master = track('MASTER')
F.tracks = {
  track('Drums'),
  track('Kick', 1, { installed('VST:ReaEQ (Cockos)'), installed('VST3:Pro-C 2 (FabFilter)') }),
  track('Vocal Lead'),
  track('Vocal Double'),
}
F.tracks[1].vals.I_FOLDERDEPTH = 1

local function copy(v)
  if type(v) ~= 'table' then return v end
  local out = {}
  for k, x in pairs(v) do out[k] = copy(x) end
  return out
end

local function touch() F.rev = F.rev + 1 end

local R = {}

function R.time_precise() return os.clock() end
function R.defer(fn) F.deferred = fn end
function R.atexit(fn) F.atexit = fn end
function R.GetExtState(section, key) return F.ext[section .. '/' .. key] or '' end
function R.SetExtState(section, key, value) F.ext[section .. '/' .. key] = value end
function R.RecursiveCreateDirectory(path) os.execute('mkdir -p "' .. path .. '"') return 1 end

function R.EnumProjects() return F, F.path end
function R.GetProjectStateChangeCount() return F.rev end
function R.GetProjectTimeSignature2() return 120, 4 end
function R.GetSetProjectInfo(_, key) return key == 'PROJECT_SRATE' and 48000 or 1 end
function R.GetAudioDeviceInfo() return false, '' end
function R.GetProjectLength() return 205.5 end
function R.IsProjectDirty() return 0 end
function R.CountTempoTimeSigMarkers() return 0 end
function R.CountProjectMarkers() return 1, 0, 1 end
-- Темп постоянный: 120 уд/мин, 4/4 — такт длится 2 с.
function R.TimeMap2_timeToBeats(_, t) return (t % 2) * 2, math.floor(t / 2), 4, t * 2, 4 end
function R.TimeMap_GetMeasureInfo(_, m) return m * 2, m * 4, (m + 1) * 4, 4, 4, 120 end
function R.TimeMap2_QNToTime(_, qn) return qn / 2 end
function R.EnumProjectMarkers3() return 1, true, 30, 60, 'Chorus', 1, 0 end

function R.Undo_BeginBlock2() F.saved = { tracks = copy(F.tracks), master = copy(F.master), rev = F.rev } end
function R.Undo_EndBlock2(_, label)
  if F.rev ~= F.saved.rev then
    F.undo[#F.undo + 1] = { label = label, state = F.saved }
    F.redo = {}  -- новая запись в истории стирает redo, как в REAPER
  end
  F.saved = nil
end
function R.Undo_CanUndo2() return F.undo[#F.undo] and F.undo[#F.undo].label or nil end
function R.Undo_CanRedo2() return F.redo[#F.redo] and F.redo[#F.redo].label or nil end
local function swap(from, to)
  local entry = table.remove(from)
  to[#to + 1] = { label = entry.label, state = { tracks = F.tracks, master = F.master } }
  F.tracks, F.master = entry.state.tracks, entry.state.master
  touch()
  return 1
end
function R.Undo_DoUndo2() return swap(F.undo, F.redo) end
function R.Undo_DoRedo2() return swap(F.redo, F.undo) end
function R.PreventUIRefresh() end
function R.UpdateArrange() end
function R.TrackList_AdjustWindows() end

function R.GetMasterTrack() return F.master end
function R.CountTracks() return #F.tracks end
function R.GetTrack(_, i) return F.tracks[i + 1] end
function R.GetTrackGUID(tr) return tr.guid end
function R.GetTrackDepth(tr) return tr.depth end
function R.GetSetMediaTrackInfo_String(tr, _, value, set)
  if set then tr.name = value touch() end
  return true, tr.name
end
function R.GetMediaTrackInfo_Value(tr, key)
  if key == 'IP_TRACKNUMBER' then
    if tr == F.master then return -1 end
    for i, t in ipairs(F.tracks) do if t == tr then return i end end
  end
  return tr.vals[key]
end
function R.SetMediaTrackInfo_Value(tr, key, value) tr.vals[key] = value touch() return true end

-- Рендер: файлов подделка не пишет, запоминает только, кто был в solo в его момент.
function R.GetSetProjectInfo_String() return true, '' end
function R.CountSelectedTracks() return 0 end
function R.SetTrackSelected() end
function R.SetOnlyTrackSelected() end
function R.CountMediaItems() return 0 end
function R.Main_OnCommand(id)
  if id ~= 42230 then return end
  local solo = {}
  for _, t in ipairs(F.tracks) do if t.vals.I_SOLO ~= 0 then solo[#solo + 1] = t.name end end
  F.renders[#F.renders + 1] = table.concat(solo, ',')
end

function R.GetTrackNumSends() return 0 end
-- Огибающие параметров плагинов: tr.envs = { { fx = 0, p = 1, name = '…' }, … }.
function R.CountTrackEnvelopes(tr) return tr.envs and #tr.envs or 0 end
function R.GetTrackEnvelope(tr, i) tr.envs[i + 1].tr = tr return tr.envs[i + 1] end
function R.GetEnvelopeName(env) return true, env.name end
function R.GetSetEnvelopeInfo_String(env, key) return true, key == 'ACTIVE' and (env.active == false and '0' or '1') or '' end
function R.CountEnvelopePoints() return 2 end
function R.CountAutomationItems() return 0 end
function R.Envelope_GetParentTrack(env) return env.tr, env.fx, env.p end
function R.CountTrackMediaItems(tr) return tr.items and #tr.items or 0 end
function R.GetTrackMediaItem(tr, i) return tr.items[i + 1] end
function R.GetMediaItemInfo_Value(item, key) return item[key] end
function R.TrackFX_GetRecCount() return 0 end
function R.Audio_IsRunning() return F.audio end
function R.Audio_Init() F.audio = 1 F.audio_log[#F.audio_log + 1] = 'init' end
function R.Audio_Quit() F.audio = 0 F.audio_log[#F.audio_log + 1] = 'quit' end

function R.TrackFX_GetCount(tr) return #tr.fx end
function R.TrackFX_GetFXName(tr, i) return true, tr.fx[i + 1].name end
function R.TrackFX_GetPreset() return false, '' end
function R.TrackFX_GetFXGUID(tr, i) return tr.fx[i + 1].guid end
function R.TrackFX_GetEnabled(tr, i) return tr.fx[i + 1].enabled end
function R.TrackFX_GetOffline(tr, i) return tr.fx[i + 1].offline end
function R.TrackFX_SetEnabled(tr, i, on) tr.fx[i + 1].enabled = on touch() end
function R.TrackFX_GetNumParams(tr, i) return #tr.fx[i + 1].params end
function R.TrackFX_GetParamName(tr, i, p) return true, tr.fx[i + 1].params[p + 1].name end
function R.TrackFX_GetParam(tr, i, p) return tr.fx[i + 1].params[p + 1].norm, 0, 1 end
function R.TrackFX_GetParamNormalized(tr, i, p) return tr.fx[i + 1].params[p + 1].norm end
function R.TrackFX_GetFormattedParamValue(tr, i, p)
  local param = tr.fx[i + 1].params[p + 1]
  return true, param.fmt(param.norm)
end
function R.TrackFX_FormatParamValueNormalized(tr, i, p, v)
  return true, tr.fx[i + 1].params[p + 1].fmt(v)
end
-- Проект меняется, когда меняется состояние плагина: у «ленивого» — не в момент записи.
local function set_param(tr, i, p, v)
  local plugin = tr.fx[i + 1]
  plugin.params[p + 1].norm = v
  if not plugin.lazy then touch() end
  return true
end
function R.TrackFX_SetParamNormalized(tr, i, p, v) return set_param(tr, i, p, v) end
function R.TrackFX_SetParam(tr, i, p, v) return set_param(tr, i, p, v) end
-- instantiate: -1 — в конец цепочки, -1000 - i — на место i (с нуля).
function R.TrackFX_AddByName(tr, name, _, instantiate)
  if name:find('Missing') then return -1 end
  local at = instantiate and instantiate <= -1000 and math.min(-1000 - instantiate, #tr.fx) or #tr.fx
  table.insert(tr.fx, at + 1, installed(name))
  touch()
  return at
end
function R.TrackFX_GetNamedConfigParm(tr, i, key)
  local plugin = tr.fx[i + 1]
  if key == 'fx_name' then return true, plugin.name end
  if key == 'fx_ident' and plugin.name:find('^JS: ') then return true, (plugin.name:gsub('^JS: ', '')) end
  if key == 'fx_ident' then return true, '/Plug-Ins/' .. plugin.name:gsub('^%w+: ', '') .. '.vst<1' end
  if key == 'vst_chunk' and plugin.lazy then
    -- Пока аудио идёт, очередной его блок переносит параметры в состояние.
    if F.audio == 1 and not plugin.deaf and plugin.saved ~= live_state(plugin) then
      plugin.saved = live_state(plugin)
      touch()
    end
    return true, plugin.saved
  end
  return false, ''
end
function R.InsertTrackAtIndex(i) table.insert(F.tracks, i + 1, track('')) touch() end
function R.DeleteTrack(tr)
  for i, t in ipairs(F.tracks) do if t == tr then table.remove(F.tracks, i) end end
  touch()
end
function R.TrackFX_Delete(tr, i) table.remove(tr.fx, i + 1) touch() return true end

function R.GetTrackStateChunk(tr)
  local lines = { '<TRACK ' .. tr.guid, 'NAME ' .. tr.name }
  for _, key in ipairs({ 'D_VOL', 'D_PAN', 'B_MUTE' }) do lines[#lines + 1] = key .. ' ' .. tr.vals[key] end
  for _, plugin in ipairs(tr.fx) do
    lines[#lines + 1] = 'FX ' .. plugin.name .. ' ' .. tostring(plugin.enabled)
    if plugin.lazy then
      lines[#lines + 1] = '  ' .. plugin.saved
    else
      for _, param in ipairs(plugin.params) do lines[#lines + 1] = string.format('  %s %.17g', param.name, param.norm) end
    end
  end
  lines[#lines + 1] = '>'
  return true, table.concat(lines, '\n')
end

R._fake = F
return R
