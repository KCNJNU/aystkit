import { CallHandler, ExecutionContext, NestInterceptor } from '@nestjs/common';
import { AuditService } from './audit.service';
export declare class AuditInterceptor implements NestInterceptor {
    private readonly audit;
    constructor(audit: AuditService);
    intercept(context: ExecutionContext, next: CallHandler): import("rxjs").Observable<any>;
}
