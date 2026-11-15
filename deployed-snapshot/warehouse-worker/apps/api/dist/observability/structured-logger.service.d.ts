import { LoggerService } from '@nestjs/common';
import { TraceContextService, TraceFields } from './trace-context.service';
type Level = 'debug' | 'log' | 'warn' | 'error' | 'verbose' | 'fatal';
export declare class StructuredLoggerService implements LoggerService {
    private readonly trace;
    constructor(trace: TraceContextService);
    log(message: unknown, ...optional: unknown[]): void;
    fatal(message: unknown, ...optional: unknown[]): void;
    error(message: unknown, ...optional: unknown[]): void;
    warn(message: unknown, ...optional: unknown[]): void;
    debug(message: unknown, ...optional: unknown[]): void;
    verbose(message: unknown, ...optional: unknown[]): void;
    event(level: Level, message: string, fields?: Partial<TraceFields> & Record<string, unknown>): void;
    private write;
}
export {};
