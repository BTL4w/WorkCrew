-- Redis TIME aligns all API processes. Only trusted server code constructs keys/rules.
-- Preflight every counter before writing. Errors/ambiguous replies never admit work;
-- partial writes on exceptional Redis failures may conservatively consume capacity.
local count = #KEYS
if count < 1 or count > 4 or #ARGV ~= count * 2 then
    return redis.error_reply('INVALID_RATE_LIMIT_RULES')
end
local now = tonumber(redis.call('TIME')[1])
local prepared = {}
local seen = {}
local denied_for = 0
for i = 1, count do
    local limit = tonumber(ARGV[(i-1)*2+1])
    local window = tonumber(ARGV[(i-1)*2+2])
    if not limit or limit < 1 or limit > 1000000 or limit ~= math.floor(limit)
        or not window or window < 1 or window > 60 or window ~= math.floor(window)
        or seen[KEYS[i]] then
        return redis.error_reply('INVALID_RATE_LIMIT_RULES')
    end
    seen[KEYS[i]] = true
    local bucket = math.floor(now / window)
    local retry = window - (now % window)
    local used = 0
    local raw = redis.call('GET', KEYS[i])
    if raw then
        local prior_bucket, prior_count = string.match(raw, '^(%d+):(%d+)$')
        prior_bucket, prior_count = tonumber(prior_bucket), tonumber(prior_count)
        local ttl = redis.call('PTTL', KEYS[i])
        if not prior_bucket or not prior_count or prior_bucket > bucket
            or prior_count > 1000000 or ttl <= 0 or ttl > window * 1000 then
            return redis.error_reply('INVALID_RATE_LIMIT_COUNTER')
        end
        if prior_bucket == bucket then used = prior_count end
    end
    if used >= limit then denied_for = math.max(denied_for, retry) end
    prepared[i] = {bucket=bucket, used=used, retry=retry}
end
if denied_for > 0 then return {0, denied_for} end
for i = 1, count do
    local item = prepared[i]
    redis.call('SET', KEYS[i], tostring(item.bucket)..':'..tostring(item.used + 1), 'EX', item.retry)
end
return {1, 0}
