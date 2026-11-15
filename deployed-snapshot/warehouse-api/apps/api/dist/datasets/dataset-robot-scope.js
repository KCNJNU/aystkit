"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.datasetRobotScope = datasetRobotScope;
function datasetRobotScope(robotId) {
    return robotId ? { OR: [
            { metadata: { path: ['robotId'], equals: robotId } },
            { files: { some: { robotId } } },
            { versions: { some: { files: { some: { robotFile: { robotId } } } } } },
        ] } : {};
}
//# sourceMappingURL=dataset-robot-scope.js.map