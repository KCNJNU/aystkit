import { PrismaService } from '../prisma/prisma.service';
import { AuthUser } from '../auth/auth.types';
export declare class SearchService {
    private readonly prisma;
    constructor(prisma: PrismaService);
    search(rawQuery: string, limit: number, scope?: AuthUser): Promise<{
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
