import { SearchService } from './search.service';
import { AuthUser } from '../auth/auth.types';
declare class SearchQueryDto {
    robotId?: string;
    q: string;
    limit: number;
}
export declare class SearchController {
    private readonly search;
    constructor(search: SearchService);
    get(query: SearchQueryDto, user: AuthUser): Promise<{
        query: string;
        groups: {
            robots: never[];
            files: {
                type: "file";
                id: string;
                title: string;
                subtitle: string;
                match: string;
            }[];
            datasets: {
                type: "dataset";
                id: string;
                title: string;
                subtitle: string;
            }[];
            versions: {
                type: "version";
                id: string;
                title: string;
                subtitle: string;
                datasetId: string;
            }[];
        };
    }>;
}
export {};
