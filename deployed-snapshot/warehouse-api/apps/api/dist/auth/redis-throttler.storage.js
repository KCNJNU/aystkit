"use strict";
var __decorate = (this && this.__decorate) || function (decorators, target, key, desc) {
    var c = arguments.length, r = c < 3 ? target : desc === null ? desc = Object.getOwnPropertyDescriptor(target, key) : desc, d;
    if (typeof Reflect === "object" && typeof Reflect.decorate === "function") r = Reflect.decorate(decorators, target, key, desc);
    else for (var i = decorators.length - 1; i >= 0; i--) if (d = decorators[i]) r = (c < 3 ? d(r) : c > 3 ? d(target, key, r) : d(target, key)) || r;
    return c > 3 && r && Object.defineProperty(target, key, r), r;
};
var __metadata = (this && this.__metadata) || function (k, v) {
    if (typeof Reflect === "object" && typeof Reflect.metadata === "function") return Reflect.metadata(k, v);
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.RedisThrottlerStorage = void 0;
const common_1 = require("@nestjs/common");
const config_1 = require("@nestjs/config");
const node_crypto_1 = require("node:crypto");
const redis_1 = require("redis");
const SCRIPT = `
local blocked = redis.call('PTTL', KEYS[2])
if blocked > 0 then return {0, redis.call('PTTL', KEYS[1]), 1, blocked} end
local hits = redis.call('INCR', KEYS[1])
if hits == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[1]) end
local ttl = redis.call('PTTL', KEYS[1])
if hits > tonumber(ARGV[2]) then
  redis.call('PSETEX', KEYS[2], ARGV[3], '1')
  return {hits, ttl, 1, tonumber(ARGV[3])}
end
return {hits, ttl, 0, 0}`;
let RedisThrottlerStorage = class RedisThrottlerStorage {
    config;
    client;
    connecting;
    constructor(config) {
        this.config = config;
    }
    async increment(key, ttl, limit, blockDuration, throttlerName) {
        try {
            const redis = await this.connection();
            const digest = (0, node_crypto_1.createHash)('sha256').update(`${this.config.getOrThrow('RATE_LIMIT_KEY_SALT')}:${throttlerName}:${key}`).digest('hex');
            const result = await redis.eval(SCRIPT, { keys: [`throttle:${digest}`, `throttle-block:${digest}`], arguments: [String(ttl), String(limit), String(blockDuration || ttl)] });
            return { totalHits: Number(result[0]), timeToExpire: Math.max(0, Number(result[1])), isBlocked: Number(result[2]) === 1, timeToBlockExpire: Math.max(0, Number(result[3])) };
        }
        catch (error) {
            if (error instanceof common_1.ServiceUnavailableException)
                throw error;
            throw new common_1.ServiceUnavailableException({ code: 'RATE_LIMIT_DEPENDENCY_UNAVAILABLE', message: 'Control operation is temporarily unavailable' });
        }
    }
    async onModuleDestroy() { if (this.client?.isOpen)
        await this.client.quit().catch(() => this.client?.destroy()); }
    async acquireAI(userId, token) {
        const redis = await this.connection();
        return (await redis.set(`ai:active:${userId}`, token, { NX: true, PX: 120000 })) === 'OK';
    }
    async releaseAI(userId, token) {
        const redis = await this.connection();
        await redis.eval("if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0", { keys: [`ai:active:${userId}`], arguments: [token] });
    }
    async connection() {
        if (this.client?.isReady)
            return this.client;
        if (!this.connecting) {
            const client = (0, redis_1.createClient)({ socket: { host: this.config.getOrThrow('REDIS_HOST'), port: Number(this.config.getOrThrow('REDIS_PORT')), connectTimeout: 3_000, reconnectStrategy: false } });
            client.on('error', () => undefined);
            this.client = client;
            this.connecting = client.connect().then(() => this.client);
        }
        try {
            return await this.connecting;
        }
        catch {
            this.connecting = undefined;
            this.client?.destroy();
            this.client = undefined;
            throw new common_1.ServiceUnavailableException({ code: 'RATE_LIMIT_DEPENDENCY_UNAVAILABLE', message: 'Control operation is temporarily unavailable' });
        }
    }
};
exports.RedisThrottlerStorage = RedisThrottlerStorage;
exports.RedisThrottlerStorage = RedisThrottlerStorage = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [config_1.ConfigService])
], RedisThrottlerStorage);
//# sourceMappingURL=redis-throttler.storage.js.map