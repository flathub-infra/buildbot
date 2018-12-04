class Home extends Controller
    constructor: ($scope, dataService, config, $location, $state, glTopbarContextualActionsService) ->
        $scope.baseurl = $location.absUrl().split("#")[0]
        $scope.config = config

        data = dataService.open().closeOnDestroy($scope)
        data.getBuilders('Builds').onNew = (builder) ->
            $scope.mainBuilder = builder
            refreshContextMenu = ->
                if $scope.$$destroyed
                    return
                actions = []
                _.forEach $scope.forceschedulers, (sch) ->
                    actions.push
                        caption: sch.button_name
                        extra_class: "btn-primary"
                        action: ->
                            $state.go("builder.forcebuilder",
                                scheduler:sch.name, builder:$scope.mainBuilder.builderid)
                glTopbarContextualActionsService.setContextualActions(actions)

            $scope.mainRunningBuilds = builder.getBuilds
                complete: false
                property: ["owners"]
                order: '-number'
            $scope.mainUpublishedBuilds = builder.getBuilds
                complete: true
                flathub_repo_status__eq: 1 # Commited
                flathub_build_type__eq: 1 # Official
                property: ["owners"]
                order: '-number'
            $scope.mainRecentBuilds = builder.getBuilds
                complete: true
                property: ["owners"]
                limit: $scope.numbuilds
                order: '-number'

             builder.getForceschedulers().onChange = (forceschedulers) ->
                $scope.forceschedulers = forceschedulers
                refreshContextMenu()
                # reinstall contextual actions when coming back from forcesched
                $scope.$on '$stateChangeSuccess', ->
                    refreshContextMenu()

        data.getBuilders('publish').onNew = (builder) ->
            $scope.publishBuilder = builder
            $scope.publishBuilds = builder.getBuilds
                complete: false
                property: ["owners"]
                order: '-number'

        data.getBuilders('purge').onNew = (builder) ->
            $scope.purgeBuilder = builder
            $scope.purgeBuilds = builder.getBuilds
                complete: false
                property: ["owners"]
                order: '-number'
