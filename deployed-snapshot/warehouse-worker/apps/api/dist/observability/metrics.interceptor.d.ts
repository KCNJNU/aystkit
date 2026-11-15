import { CallHandler, ExecutionContext, NestInterceptor } from '@nestjs/common';
import { Observable } from 'rxjs';
import { MetricsService } from './metrics.service';
import { StructuredLoggerService } from './structured-logger.service';
import { TraceContextService } from './trace-context.service';
export declare class MetricsInterceptor implements NestInterceptor {
    private readonly metrics;
    private readonly trace;
    private readonly logger;
    constructor(metrics: MetricsService, trace: TraceContextService, logger: StructuredLoggerService);
    intercept(context: ExecutionContext, next: CallHandler): Observable<unknown>;
}
