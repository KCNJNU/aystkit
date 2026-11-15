import { Response } from 'express';
import { HealthService } from './health.service';
export declare class HealthController {
    private readonly health;
    constructor(health: HealthService);
    liveness(): {
        status: string;
        timestamp: string;
    };
    ready(response: Response): Promise<{
        status: string;
        timestamp: string;
    }>;
    status(): Promise<import("./health.service").HealthReport>;
}
