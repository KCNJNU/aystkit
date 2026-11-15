import { NestMiddleware } from '@nestjs/common';
import type { NextFunction, Response } from 'express';
import type { AuthenticatedRequest } from '../auth/auth.types';
import { TraceContextService } from './trace-context.service';
export declare class TraceMiddleware implements NestMiddleware {
    private readonly context;
    constructor(context: TraceContextService);
    use(request: AuthenticatedRequest, response: Response, next: NextFunction): void;
}
