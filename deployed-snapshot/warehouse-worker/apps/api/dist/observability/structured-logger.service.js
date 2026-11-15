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
exports.StructuredLoggerService = void 0;
const common_1 = require("@nestjs/common");
const trace_context_service_1 = require("./trace-context.service");
let StructuredLoggerService = class StructuredLoggerService {
    trace;
    constructor(trace) {
        this.trace = trace;
    }
    log(message, ...optional) { this.write('log', message, context(optional)); }
    fatal(message, ...optional) { this.write('fatal', message, context(optional)); }
    error(message, ...optional) { this.write('error', message, context(optional)); }
    warn(message, ...optional) { this.write('warn', message, context(optional)); }
    debug(message, ...optional) { this.write('debug', message, context(optional)); }
    verbose(message, ...optional) { this.write('verbose', message, context(optional)); }
    event(level, message, fields = {}) {
        this.write(level, message, undefined, fields);
    }
    write(level, message, loggerContext, fields = {}) {
        const record = redact({
            timestamp: new Date().toISOString(), level, service: process.env.RUNTIME_SERVICE_NAME ?? 'unknown',
            instance: process.env.COMPUTERNAME ?? process.env.HOSTNAME ?? 'local', ...this.trace.get(), ...fields,
            context: loggerContext, message: printable(message),
        });
        const line = `${JSON.stringify(record)}\n`;
        if (level === 'error' || level === 'fatal')
            process.stderr.write(line);
        else
            process.stdout.write(line);
    }
};
exports.StructuredLoggerService = StructuredLoggerService;
exports.StructuredLoggerService = StructuredLoggerService = __decorate([
    (0, common_1.Injectable)(),
    __metadata("design:paramtypes", [trace_context_service_1.TraceContextService])
], StructuredLoggerService);
function context(optional) { const last = optional.at(-1); return typeof last === 'string' ? last : undefined; }
function printable(value) { if (value instanceof Error)
    return value.message; if (typeof value === 'string')
    return value; try {
    return JSON.stringify(value);
}
catch {
    return String(value);
} }
function redact(value) {
    const result = {};
    for (const [key, item] of Object.entries(value)) {
        if (/password|token|secret|authorization|private.?key/i.test(key))
            result[key] = '[REDACTED]';
        else if (typeof item === 'string')
            result[key] = item.replace(/([?&](?:password|token|secret)=)[^&\s]+/gi, '$1[REDACTED]').replace(/:\/\/([^:/\s]+):([^@/\s]+)@/g, '://$1:[REDACTED]@');
        else
            result[key] = item;
    }
    return result;
}
//# sourceMappingURL=structured-logger.service.js.map