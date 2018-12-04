class Apps extends Controller
    constructor: ($scope, $log, dataService, resultsService, bbSettingsService, $stateParams,
        $location, dataGrouperService, $rootScope, $filter) ->
        # make resultsService utilities available in the template
        _.mixin($scope, resultsService)
        data = dataService.open().closeOnDestroy($scope)
        data.getBuilders('Builds').onNew = (builder) ->
            $scope.mainBuilder = builder
            if $stateParams.numbuilds?
                $scope.numbuilds = +$stateParams.numbuilds
            $scope.builds = builder.getBuilds
                flathub_build_type__eq: 1 # Official
                property: ["owners", "unique-apps"]
                order: ['flathub_name', '-number']
