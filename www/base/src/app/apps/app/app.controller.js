class AppController {
    constructor($rootScope, $scope, dataService, $stateParams, resultsService,
        glBreadcrumbService, $state, glTopbarContextualActionsService, $q, $window) {
        // make resultsService utilities available in the template
        _.mixin($scope, resultsService);
        const appname = $stateParams.app;
        $scope.appname=appname;
        const data = dataService.open().closeOnDestroy($scope);
        data.getBuilders('Builds').onNew = function(builder) {
            $scope.builder = builder;
            const breadcrumb = [{
                    caption: "Apps",
                    sref: "apps"
                }
                , {
                    caption: appname,
                    sref: "app({app:'" + appname + "'})"
                }
            ];
            glBreadcrumbService.setBreadcrumb(breadcrumb);

            var refreshContextMenu = function() {
                if ($scope.$$destroyed) {
                    return;
                }
                const actions = [
                ];
                _.forEach($scope.forceschedulers, sch =>
                    actions.push({
                        caption: sch.button_name,
                        extra_class: "btn-primary",
                        action() {
                            return $state.go("builder.forcebuilder",
                                {scheduler:sch.name, builder:$scope.builder.builderid, buildname:$scope.appname});
                        }
                    })
                );

                return glTopbarContextualActionsService.setContextualActions(actions);
            };

            builder.getForceschedulers({order:'name'}).onChange = function(forceschedulers) {
                $scope.forceschedulers = forceschedulers;
                refreshContextMenu();
                // reinstall contextual actions when coming back from forcesched
                return $scope.$on('$stateChangeSuccess', () => refreshContextMenu());
            };
            $scope.numbuilds = 100;
            if ($stateParams.numbuilds != null) {
                $scope.numbuilds = +$stateParams.numbuilds;
            }
            $scope.runningBuilds = builder.getBuilds({
                flathub_name__eq: appname,
                complete: false,
                property: ["owners", "workername"],
                order: '-number'
            });
            $scope.unpublishedBuilds = builder.getBuilds({
                flathub_name__eq: appname,
                complete: true,
                flathub_repo_status__le: 1,
                property: ["owners", "workername"],
                order: '-number'
            });
            $scope.recentBuilds = builder.getBuilds({
                flathub_name__eq: appname,
                complete: true,
                flathub_repo_status__gt: 1,
                property: ["owners", "workername"],
                limit: $scope.numbuilds,
                order: '-number'
            });

            $scope.changeCount = 0;
            const refreshGraph = function () {
                $scope.changeCount = $scope.changeCount + 1;
                if ($scope.changeCount == 1) { // We wait until both unpublished and recent are fetched
                        return;
                }
                $scope.successful_builds = [];
                const addBuild = function(b) {
                    if (b.results == 0 && b.complete_at != null) {
                        b.duration = b.complete_at - b.started_at;
                        $scope.successful_builds.push(b);
                    }
                };
                $scope.unpublishedBuilds.forEach(addBuild);
                $scope.recentBuilds.forEach(addBuild);
                $scope.successful_builds.sort( (a,b) => (a.started_at >= b.started_at) ? 1 : -1 );
            };
            $scope.recentBuilds.onChange = function() {
                refreshGraph();
            };
            $scope.unpublishedBuilds.onChange = function() {
                refreshGraph();
            };
        };
    }
}

angular.module('app')
.controller('appController', ['$rootScope', '$scope', 'dataService', '$stateParams', 'resultsService', 'glBreadcrumbService', '$state', 'glTopbarContextualActionsService', '$q', '$window', AppController]);
