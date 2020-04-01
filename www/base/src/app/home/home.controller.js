/*
 * decaffeinate suggestions:
 * DS102: Remove unnecessary code created because of implicit returns
 * DS207: Consider shorter variations of null checks
 * Full docs: https://github.com/decaffeinate/decaffeinate/blob/master/docs/suggestions.md
 */
class Home {
    constructor($scope, dataService, config, $location, $state, glTopbarContextualActionsService) {
        $scope.baseurl = $location.absUrl().split("#")[0];
        $scope.config = config;

        const data = dataService.open().closeOnDestroy($scope);
        data.getBuilders('Builds').onNew = function(builder) {
            $scope.mainBuilder = builder;
            var refreshContextMenu = function () {
                if ($scope.$$destroyed) {
                    return;
                }
                const actions = [];
                _.forEach($scope.forceschedulers, sch =>
                    actions.push({
                        caption: sch.button_name,
                        extra_class: "btn-primary",
                        action() {
                            return $state.go("builder.forcebuilder", {
                                    scheduler: sch.name,
                                    builder:$scope.mainBuilder.builderid
                                });
                        }
                    })
                );

                return glTopbarContextualActionsService.setContextualActions(actions);
            };

            $scope.mainRunningBuilds = builder.getBuilds({
                complete: false,
                property: ["owners"],
                order: '-number'
            });

            $scope.mainUpublishedBuilds = builder.getBuilds({
                complete: true,
                flathub_repo_status__eq: 1, // Commited
                flathub_build_type__eq: 1, // Official
                property: ["owners"],
                order: '-number'
            });

            $scope.mainRecentBuilds = builder.getBuilds({
                complete: true,
                property: ["owners"],
                limit: 50,
                order: '-number'
            });

            builder.getForceschedulers().onChange = function(forceschedulers) {
                $scope.forceschedulers = forceschedulers;
                refreshContextMenu();
                // reinstall contextual actions when coming back from forcesched
                return $scope.$on('$stateChangeSuccess', () => refreshContextMenu());
            };
        };

        data.getBuilders('publish').onNew = function(builder) {
            $scope.publishBuilder = builder;
            $scope.publishBuilds = builder.getBuilds({
                complete: false,
                property: ["owners"],
                order: '-number'
            });
        };

        data.getBuilders('purge').onNew = function(builder) {
            $scope.purgeBuilder = builder;
            $scope.purgeBuilds = builder.getBuilds({
                complete: false,
                property: ["owners"],
                order: '-number'
            });
        };
    }
}


angular.module('app')
.controller('homeController', ['$scope', 'dataService', 'config', '$location', '$state', 'glTopbarContextualActionsService', Home]);
