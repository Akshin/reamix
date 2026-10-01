-- reamix: плагины для переноса проекта из Ableton (reamix import-als).
-- Проект REAPER пишется текстом, но блок плагина с его состоянием умеет собрать только
-- живой REAPER. Цепочки собираются здесь, на временных треках в пустой вкладке-верстаке:
-- плагины добавляются, получают состояние из Ableton и автоматизацию параметров, готовый
-- блок <FXCHAIN уходит в файл. Экземпляры живут до конца импорта: плагин, созданный и
-- удалённый за один вызов, может уронить REAPER своим отложенным обработчиком.
-- Загружается запросом: loadfile(путь)(rx, A).

local rx, A = ...
local r = reaper

local M = {}

local Failure = { __tostring = function(e) return e.reamix end }
local function fail(msg) error(setmetatable({ reamix = msg }, Failure), 0) end

local function read_file(path)
  local f = io.open(path, 'rb') or fail('файл не открывается: ' .. path)
  local data = f:read('a')
  f:close()
  return data
end

-- ── base64 ────────────────────────────────────────────────────────────────

local ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
local ENC, DEC = {}, {}
for i = 1, 64 do
  ENC[i - 1] = ALPHABET:sub(i, i)
  DEC[ALPHABET:byte(i)] = i - 1
end

function M.b64_decode(text)
  text = text:gsub('[^%w%+/]', '')
  local out, n = {}, 0
  for i = 1, #text - 3, 4 do
    local a, b, c, d = text:byte(i, i + 3)
    local v = DEC[a] << 18 | DEC[b] << 12 | DEC[c] << 6 | DEC[d]
    n = n + 1
    out[n] = string.char(v >> 16, v >> 8 & 255, v & 255)
  end
  local rest = #text % 4
  if rest > 1 then
    local a, b, c = text:byte(#text - rest + 1, #text)
    local v = DEC[a] << 18 | DEC[b] << 12 | (c and DEC[c] << 6 or 0)
    n = n + 1
    out[n] = rest == 3 and string.char(v >> 16, v >> 8 & 255) or string.char(v >> 16)
  end
  return table.concat(out)
end

function M.b64_encode(bytes)
  local out, n = {}, 0
  for i = 1, #bytes - 2, 3 do
    local a, b, c = bytes:byte(i, i + 2)
    local v = a << 16 | b << 8 | c
    n = n + 1
    out[n] = ENC[v >> 18] .. ENC[v >> 12 & 63] .. ENC[v >> 6 & 63] .. ENC[v & 63]
  end
  local rest = #bytes % 3
  if rest == 1 then
    local v = bytes:byte(#bytes) << 16
    n = n + 1
    out[n] = ENC[v >> 18] .. ENC[v >> 12 & 63] .. '=='
  elseif rest == 2 then
    local a, b = bytes:byte(#bytes - 1, #bytes)
    local v = a << 16 | b << 8
    n = n + 1
    out[n] = ENC[v >> 18] .. ENC[v >> 12 & 63] .. ENC[v >> 6 & 63] .. '='
  end
  return table.concat(out)
end

-- ── Состояние AU ──────────────────────────────────────────────────────────

-- Блок AU в чанке трека: версия, раскладка входов и выходов, длина состояния, само
-- состояние (plist, как его отдаёт плагин), хвост. Ableton хранит тот же plist — меняем
-- только его, остальное REAPER уже заполнил сам, когда добавлял плагин.
function M.au_with_state(block, plist)
  local _, _, n_in = string.unpack('<I4I4I4', block)
  local at = 13 + n_in * 8
  local n_out = string.unpack('<I4', block, at)
  at = at + 4 + n_out * 8
  local old_len = string.unpack('<I4', block, at)
  if at + 3 + old_len > #block then fail('блок AU устроен не так, как ожидалось') end
  return block:sub(1, at - 1) .. string.pack('<I4', #plist) .. plist .. block:sub(at + 4 + old_len)
end

local FX_BLOCKS = { VST = true, AU = true, JS = true, CLAP = true, DX = true, LV2 = true, VIDEO_EFFECT = true, CONTAINER = true }

-- Заменяет состояние AU-плагинов в чанке трека. states: номер плагина в цепочке (с 0) → plist.
function M.patch_au(chunk, states)
  local out, n = {}, 0
  local depth, chain_depth, fx, body = 0, nil, -1, nil
  for line in (chunk .. '\n'):gmatch('(.-)\r?\n') do
    local text = line:match('^%s*(.-)%s*$')
    local tag = text:match('^<([%w_]+)')
    local keep = true
    if body then
      if text == '>' then
        local raw = M.au_with_state(M.b64_decode(table.concat(body.lines)), body.plist)
        local encoded = M.b64_encode(raw)
        for i = 1, #encoded, 128 do
          n = n + 1
          out[n] = encoded:sub(i, i + 127)
        end
        body = nil
        depth = depth - 1
      else
        body.lines[#body.lines + 1] = text
        keep = false
      end
    elseif tag then
      if tag == 'FXCHAIN' and not chain_depth then
        chain_depth = depth
      elseif chain_depth and depth == chain_depth + 1 and FX_BLOCKS[tag] then
        fx = fx + 1
        if tag == 'AU' and states[fx] then body = { lines = {}, plist = states[fx] } end
      end
      depth = depth + 1
    elseif text == '>' then
      depth = depth - 1
      if chain_depth and depth == chain_depth then chain_depth = false end
    end
    if keep then
      n = n + 1
      out[n] = line
    end
  end
  return table.concat(out, '\n')
end

-- ── Плагины и их автоматизация ────────────────────────────────────────────

local function param_named(tr, fx, name)
  local loose
  for p = 0, r.TrackFX_GetNumParams(tr, fx) - 1 do
    local _, pname = r.TrackFX_GetParamName(tr, fx, p, '')
    if pname == name then return p end
    if not loose and pname:lower() == name:lower() then loose = p end
  end
  return loose
end

-- Точки из файла (время, значение 0..1, форма) в огибающую параметра p.
local function write_envelope(tr, fx, p, path)
  local _, lo, hi = r.TrackFX_GetParam(tr, fx, p)
  local env = r.GetFXEnvelope(tr, fx, p, true)
  if not env then return 0 end
  r.DeleteEnvelopePointRange(env, -1, 1e9)
  local count = 0
  for line in read_file(path):gmatch('[^\r\n]+') do
    local t, v, shape = line:match('^(%S+)\t(%S+)\t(%S+)')
    if tonumber(t) then
      r.InsertEnvelopePoint(env, tonumber(t), lo + tonumber(v) * (hi - lo), tonumber(shape), 0, false, true)
      count = count + 1
    end
  end
  r.Envelope_SortPoints(env)
  return count
end

-- Блок <FXCHAIN ... > из чанка трека; nil, если плагинов на треке нет.
function M.chain_of(chunk)
  local lines, depth = nil, 0
  for line in (chunk .. '\n'):gmatch('(.-)\r?\n') do
    local text = line:match('^%s*(.-)%s*$')
    if not lines and text == '<FXCHAIN' then lines = {} end
    if lines then
      lines[#lines + 1] = text
      if text:sub(1, 1) == '<' then depth = depth + 1 elseif text == '>' then depth = depth - 1 end
      if depth == 0 then return table.concat(lines, '\n') end
    end
  end
end

-- ── Верстак ───────────────────────────────────────────────────────────────

-- Что открыто в REAPER. Импорт грузит плагины в этом же процессе: несохранённая работа
-- в любой вкладке под угрозой, если плагин упадёт.
function M.host()
  local projects = A {}
  local i = 0
  while true do
    local proj, path = r.EnumProjects(i)
    if not proj then break end
    local _, mark = r.GetProjExtState(proj, 'reamix', 'workbench')
    -- workbench: вкладка осталась от прерванного импорта — в ней только временные треки.
    projects[#projects + 1] = { path = path or '', dirty = r.IsProjectDirty(proj) == 1, tracks = r.CountTracks(proj), workbench = mark ~= '' }
    i = i + 1
  end
  local _, active = r.EnumProjects(-1)
  local _, mark = r.GetProjExtState(0, 'reamix', 'workbench')
  return {
    projects = projects, active = active or '', active_tracks = r.CountTracks(0),
    workbench = mark ~= '',  -- активна вкладка прерванного импорта: её можно занять снова
    version = r.GetAppVersion(),
  }
end

-- Помечает активную пустую вкладку верстаком импорта. Вкладку открывает человек: действие
-- «New project tab» показывает окно свойств проекта, а оно останавливает мост.
function M.workbench(token)
  if r.CountTracks(0) > 0 then
    local _, mark = r.GetProjExtState(0, 'reamix', 'workbench')
    if mark == '' then fail('активная вкладка REAPER не пустая') end
    -- Временные треки прерванного импорта: их плагины уже пожили, удалять безопасно.
    for i = r.CountTracks(0) - 1, 0, -1 do r.DeleteTrack(r.GetTrack(0, i)) end
  end
  r.SetProjExtState(0, 'reamix', 'workbench', token)
  return true
end

local function on_workbench(token)
  local _, value = r.GetProjExtState(0, 'reamix', 'workbench')
  if value ~= token then
    fail('активная вкладка REAPER сменилась посреди импорта: плагины ставятся только во вкладку импорта')
  end
end

-- Заголовок блока VST в чанке: после идентификатора плагина идёт имя архива ARA — пустое
-- у обычного плагина. По нему видно, что REAPER загрузил плагин через ARA.
function M.is_ara_header(header)
  local tail = header:match('}%s*(.-)%s*$')
  return tail ~= nil and tail ~= '' and tail ~= '""'
end

-- Какие из плагинов REAPER грузит через ARA (Melodyne, VocAlign, Vovious). Такой плагин
-- состояние из Ableton не примет, а второй на той же дорожке REAPER не вставит и
-- остановится на модальном окне. Каждый кандидат ставится один на свой трек верстака —
-- так окна не бывает. Возвращает имя → true/false; не добавившиеся в ответ не попадают.
function M.probe_ara(token, names)
  on_workbench(token)
  local out = {}
  for _, name in ipairs(names) do
    local n = r.CountTracks(0)
    r.InsertTrackAtIndex(n, false)
    local tr = r.GetTrack(0, n)
    if r.TrackFX_AddByName(tr, name, false, -1) >= 0 then
      local _, chunk = r.GetTrackStateChunk(tr, '', false)
      local header = chunk:match('\n%s*(<VST [^\n]*)')
      out[name] = header ~= nil and M.is_ara_header(header)
    end
  end
  return out
end

-- Собирает цепочку плагинов на новом треке верстака и пишет её блок <FXCHAIN в файл out.
-- Запись списка entries:
--   add — имена для TrackFX_AddByName по убыванию предпочтения; fmt — 'VST3' | 'VST' | 'AU';
--   state — файл состояния (VST: готовый base64 для vst_chunk; AU: plist); on — включён ли;
--   envelopes — { {param = имя, file = точки}, ... }; bypass — файл точек огибающей bypass.
-- channels — число каналов трека (4, если на него приходит сайдчейн).
-- Возвращает по записи: name либо error; state = true/false; envelopes; no_param.
function M.build_chain(token, entries, out, channels)
  on_workbench(token)
  local n = r.CountTracks(0)
  r.InsertTrackAtIndex(n, false)
  local tr = r.GetTrack(0, n)
  r.SetMediaTrackInfo_Value(tr, 'I_NCHAN', channels or 2)
  local results, placed, au = A {}, {}, {}
  for i, e in ipairs(entries) do
    local res, fx = {}, -1
    for _, name in ipairs(e.add) do
      fx = r.TrackFX_AddByName(tr, name, false, -1)
      if fx >= 0 then break end
    end
    if fx < 0 then
      res.error = 'REAPER не смог добавить плагин'
    else
      placed[i] = fx
      res.name = ({ r.TrackFX_GetFXName(tr, fx, '') })[2]
      if e.state and e.fmt == 'AU' then
        au[fx] = read_file(e.state)
      elseif e.state then
        res.state = r.TrackFX_SetNamedConfigParm(tr, fx, 'vst_chunk', read_file(e.state))
      end
    end
    results[i] = res
  end

  if next(au) then
    local ok, chunk = r.GetTrackStateChunk(tr, '', false)
    local patched_ok, patched = pcall(M.patch_au, chunk, au)
    local set = ok and patched_ok and r.SetTrackStateChunk(tr, patched, false)
    for i, fx in pairs(placed) do
      if au[fx] then results[i].state = set and true or false end
    end
  end

  for i, e in ipairs(entries) do
    local fx, res = placed[i], results[i]
    if fx then
      r.TrackFX_SetEnabled(tr, fx, e.on)
      local missing = A {}
      for _, env in ipairs(e.envelopes or {}) do
        local p = param_named(tr, fx, env.param)
        if p and write_envelope(tr, fx, p, env.file) > 0 then
          res.envelopes = (res.envelopes or 0) + 1
        else
          missing[#missing + 1] = env.param
        end
      end
      if e.bypass then
        local p = r.TrackFX_GetParamFromIdent(tr, fx, ':bypass')
        if p >= 0 and write_envelope(tr, fx, p, e.bypass) > 0 then
          res.envelopes = (res.envelopes or 0) + 1
        else
          missing[#missing + 1] = 'вкл/выкл'
        end
      end
      if #missing > 0 then res.no_param = missing end
    end
  end

  local ok, chunk = r.GetTrackStateChunk(tr, '', false)
  local chain = ok and M.chain_of(chunk)
  if chain then
    local f = io.open(out, 'wb') or fail('файл не пишется: ' .. out)
    f:write(chain)
    f:close()
  end
  return { fx = results, written = chain ~= nil }
end

-- Открывает готовый проект на месте верстака и сверяет, что REAPER его принял:
-- файлы, которые не открылись, и время, в которое он ставит доли qn.
function M.open(token, path, qn)
  on_workbench(token)
  r.Main_openProject('noprompt:' .. path)
  local _, now = r.EnumProjects(-1)
  if (now or ''):lower() ~= path:lower() then fail('REAPER не открыл проект: ' .. path) end
  local offline = {}
  for i = 0, r.CountMediaItems(0) - 1 do
    local take = r.GetActiveTake(r.GetMediaItem(0, i))
    local src = take and r.GetMediaItemTake_Source(take)
    if src and r.GetMediaSourceType(src, '') ~= 'MIDI' and r.GetMediaSourceLength(src) == 0 then
      offline[r.GetMediaSourceFileName(src, '')] = true
    end
  end
  local files, times, fx = A {}, A {}, 0
  for name in pairs(offline) do files[#files + 1] = name end
  table.sort(files)
  for i, beat in ipairs(qn or {}) do times[i] = r.TimeMap2_QNToTime(0, beat) end
  for i = 0, r.CountTracks(0) - 1 do fx = fx + r.TrackFX_GetCount(r.GetTrack(0, i)) end
  fx = fx + r.TrackFX_GetCount(r.GetMasterTrack(0))
  -- Сохраняем сразу: на диске оказывается проект в полном виде, каким его держит REAPER.
  r.Main_SaveProject(0, false)
  return {
    project = now, tracks = r.CountTracks(0), items = r.CountMediaItems(0), fx = fx, offline = files, times = times,
    saved = r.IsProjectDirty(0) == 0,
  }
end

-- Убирает временные треки, если проект в верстаке открывать не нужно.
function M.cleanup(token)
  on_workbench(token)
  for i = r.CountTracks(0) - 1, 0, -1 do r.DeleteTrack(r.GetTrack(0, i)) end
  return true
end

return M
