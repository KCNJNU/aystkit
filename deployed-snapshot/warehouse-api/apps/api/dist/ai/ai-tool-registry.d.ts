import { AuthUser } from '../auth/auth.types';
import { HealthService } from '../health/health.service';
import { AITool } from './ai-provider';
import { QueryResult, WarehouseQueryService } from '../warehouse/warehouse-query.service';
export declare class AIToolRegistry {
    private readonly warehouse;
    private readonly health;
    constructor(warehouse: WarehouseQueryService, health: HealthService);
    readonly tools: AITool[];
    execute(name: string, raw: string, user: AuthUser): Promise<QueryResult>;
}
