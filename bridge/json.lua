-- Кодировщик Lua → JSON. Числа пишутся без потери точности (%.17g).
-- Пустая таблица — объект {}; список помечается явно: json.array{...}.

local json = {}

local ARRAY = {}

function json.array(t)
  return setmetatable(t or {}, ARRAY)
end

local escapes = {
  ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
  ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function escape_char(c)
  return escapes[c] or string.format('\\u%04x', c:byte())
end

local function encode_string(s)
  -- Диапазон байтов задан явно: класс %c зависит от локали и задевает UTF-8.
  return '"' .. s:gsub('[\0-\31"\\]', escape_char) .. '"'
end

local function encode_number(n)
  if math.type(n) == 'integer' then return string.format('%d', n) end
  if n ~= n or n == math.huge or n == -math.huge then return 'null' end
  return string.format('%.17g', n)
end

local function is_array(t)
  if getmetatable(t) == ARRAY then return true end
  local n = #t
  if n == 0 then return false end
  local count = 0
  for _ in pairs(t) do count = count + 1 end
  return count == n
end

local encode

local function encode_table(t, seen, out)
  if seen[t] then error('json: циклическая ссылка', 0) end
  seen[t] = true
  if is_array(t) then
    out[#out + 1] = '['
    for i = 1, #t do
      if i > 1 then out[#out + 1] = ',' end
      encode(t[i], seen, out)
    end
    out[#out + 1] = ']'
  else
    local keys = {}
    for k in pairs(t) do keys[#keys + 1] = tostring(k) end
    table.sort(keys)
    local by_name = {}
    for k, v in pairs(t) do by_name[tostring(k)] = v end
    out[#out + 1] = '{'
    for i, k in ipairs(keys) do
      if i > 1 then out[#out + 1] = ',' end
      out[#out + 1] = encode_string(k)
      out[#out + 1] = ':'
      encode(by_name[k], seen, out)
    end
    out[#out + 1] = '}'
  end
  seen[t] = nil
end

encode = function(v, seen, out)
  local tv = type(v)
  if tv == 'nil' then out[#out + 1] = 'null'
  elseif tv == 'boolean' then out[#out + 1] = v and 'true' or 'false'
  elseif tv == 'number' then out[#out + 1] = encode_number(v)
  elseif tv == 'string' then out[#out + 1] = encode_string(v)
  elseif tv == 'table' then encode_table(v, seen, out)
  else out[#out + 1] = encode_string(tostring(v)) end
end

function json.encode(v)
  local out = {}
  encode(v, {}, out)
  return table.concat(out)
end

return json
