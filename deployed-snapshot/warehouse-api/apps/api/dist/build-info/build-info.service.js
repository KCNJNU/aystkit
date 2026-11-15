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
exports.BuildInfoService = void 0;
const common_1 = require("@nestjs/common");
const config_1 = require("@nestjs/config");
let BuildInfoService = class BuildInfoService {
    config;
    constructor(config) {
        this.config = config;
    }
    get() {
        return {
            service: safe(this.config.get('RUNTIME_SERVICE_NAME') ?? process.env.RUNTIME_SERVICE_NAME, 'unknown'),
            version: semver(this.config.get('APP_VERSION')),
            gitCommit: commit(this.config.get('GIT_COMMIT')),
            buildTime: timestamp(this.config.get('BUILD_TIME')),
        };
    }
};
exports.BuildInfoService = BuildInfoService;
exports.BuildInfoService = BuildInfoService = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [config_1.ConfigService])
], BuildInfoService);
function safe(value, fallback) { return value && /^[A-Za-z0-9._-]{1,64}$/.test(value) ? value : fallback; }
function semver(value) { return value && /^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(value) ? value : '0.0.0-dev'; }
function commit(value) { return value && /^[0-9a-f]{7,40}$/i.test(value) ? value.toLowerCase() : 'unknown'; }
function timestamp(value) { return value && !Number.isNaN(Date.parse(value)) ? new Date(value).toISOString() : 'unknown'; }
//# sourceMappingURL=build-info.service.js.map