import { OnModuleInit } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
export declare class ProductionSecurityValidator implements OnModuleInit {
    private readonly config;
    constructor(config: ConfigService);
    onModuleInit(): void;
    private required;
}
